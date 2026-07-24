#include "CDNAudioFile.h"

#include <string.h>          // for memcpy
#include <functional>        // for __base
#include <initializer_list>  // for initializer_list
#include <map>               // for operator!=, operator==
#include <stdexcept>         // for runtime_error
#include <string_view>       // for string_view
#include <type_traits>       // for remove_extent_t

#include "AccessKeyFetcher.h"  // for AccessKeyFetcher
#include "BellLogger.h"        // for AbstractLogger
#include "Crypto.h"
#include "Logger.h"            // for CSPOT_LOG
#include "Packet.h"            // for cspot
#include "SocketStream.h"      // for SocketStream
#include "Utils.h"             // for bigNumAdd, bytesToHexString, string...
#include "WrappedSemaphore.h"  // for WrappedSemaphore
#ifdef BELL_ONLY_CJSON
#include "cJSON.h"
#else
#include "nlohmann/json.hpp"      // for basic_json<>::object_t, basic_json
#include "nlohmann/json_fwd.hpp"  // for json
#endif

using namespace cspot;

CDNAudioFile::CDNAudioFile(const std::string& cdnUrl,
                           const std::vector<uint8_t>& audioKey)
    : cdnUrl(cdnUrl), audioKey(audioKey) {
  this->crypto = std::make_unique<Crypto>();
}

size_t CDNAudioFile::getPosition() {
  return this->position;
}

void CDNAudioFile::seek(size_t newPos) {
  this->enableRequestMargin = true;
  this->position = newPos;
}

// ADI PATCH: HTTP errors from the CDN (403/404/416...) used to be treated as audio data,
// and a short error body made `totalLength() - SPOTIFY_OPUS_HEADER` underflow into a
// multi-gigabyte footer allocation. Check the status and sizes before trusting them.
static void requireHttpOk(const bell::HTTPClient::Response& response,
                          const char* what) {
  if (response.statusCode != 200 && response.statusCode != 206) {
    throw std::runtime_error(std::string("CDN returned HTTP ") +
                             std::to_string(response.statusCode) +
                             " for the " + what);
  }
}

void CDNAudioFile::openStream() {
  CSPOT_LOG(info, "Opening HTTP stream to %s", this->cdnUrl.c_str());

  // Open connection, read the first OPUS_HEADER_SIZE bytes
  this->httpConnection = bell::HTTPClient::get(
      this->cdnUrl,
      {bell::HTTPClient::RangeHeader::range(0, OPUS_HEADER_SIZE - 1)});
  requireHttpOk(*this->httpConnection, "header");

  this->httpConnection->stream().read((char*)header.data(), OPUS_HEADER_SIZE);
  if (this->httpConnection->stream().gcount() != (std::streamsize)OPUS_HEADER_SIZE) {
    throw std::runtime_error(
        "short read of the CDN header (" +
        std::to_string((long)this->httpConnection->stream().gcount()) + " of " +
        std::to_string(OPUS_HEADER_SIZE) + " bytes)");
  }

  const size_t fullFileSize = this->httpConnection->totalLength();
  if (fullFileSize < (size_t)SPOTIFY_OPUS_HEADER + (size_t)OPUS_FOOTER_PREFFERED) {
    throw std::runtime_error("CDN reported an implausible file size (" +
                             std::to_string(fullFileSize) + " bytes)");
  }
  this->totalFileSize = fullFileSize - SPOTIFY_OPUS_HEADER;

  this->decrypt(header.data(), OPUS_HEADER_SIZE, 0);

  // Location must be dividable by 16
  size_t footerStartLocation =
      (this->totalFileSize - OPUS_FOOTER_PREFFERED + SPOTIFY_OPUS_HEADER) -
      (this->totalFileSize - OPUS_FOOTER_PREFFERED + SPOTIFY_OPUS_HEADER) % 16;

  this->footer = std::vector<uint8_t>(
      this->totalFileSize - footerStartLocation + SPOTIFY_OPUS_HEADER);
  this->httpConnection->get(
      cdnUrl, {bell::HTTPClient::RangeHeader::last(footer.size())});
  requireHttpOk(*this->httpConnection, "footer");

  this->httpConnection->stream().read((char*)footer.data(),
                                      this->footer.size());
  if (this->httpConnection->stream().gcount() != (std::streamsize)this->footer.size()) {
    throw std::runtime_error("short read of the CDN footer");
  }

  this->decrypt(footer.data(), footer.size(), footerStartLocation);
  CSPOT_LOG(info, "Header and footer bytes received (file %u bytes)",
            (unsigned int)fullFileSize);
  this->position = 0;
  this->lastRequestPosition = 0;
  this->lastRequestCapacity = 0;
}

size_t CDNAudioFile::readBytes(uint8_t* dst, size_t bytes) {
  size_t offsetPosition = position + SPOTIFY_OPUS_HEADER;
  size_t actualFileSize = this->totalFileSize + SPOTIFY_OPUS_HEADER;

  if (position + bytes >= this->totalFileSize) {
    return 0;
  }

  // // Opus tries to read header, use prefetched data
  if (offsetPosition < OPUS_HEADER_SIZE &&
      bytes + offsetPosition <= OPUS_HEADER_SIZE) {
    memcpy(dst, this->header.data() + offsetPosition, bytes);
    position += bytes;
    return bytes;
  }

  // // Opus tries to read footer, use prefetched data
  if (offsetPosition >= (actualFileSize - this->footer.size())) {
    size_t toReadBytes = bytes;

    if ((position + bytes) > this->totalFileSize) {
      // Tries to read outside of bounds, truncate
      toReadBytes = this->totalFileSize - position;
    }

    size_t footerOffset =
        offsetPosition - (actualFileSize - this->footer.size());
    memcpy(dst, this->footer.data() + footerOffset, toReadBytes);

    position += toReadBytes;
    return toReadBytes;
  }

  // Data not in the headers. Make sense of whats going on.
  // Position in bounds :)
  if (offsetPosition >= this->lastRequestPosition &&
      offsetPosition < this->lastRequestPosition + this->lastRequestCapacity) {
    size_t toRead = bytes;

    if ((toRead + offsetPosition) >
        this->lastRequestPosition + lastRequestCapacity) {
      toRead = this->lastRequestPosition + lastRequestCapacity - offsetPosition;
    }

    memcpy(dst, this->httpBuffer.data() + offsetPosition - lastRequestPosition,
           toRead);
    position += toRead;

    return toRead;
  } else {
    size_t requestPosition = (offsetPosition) - ((offsetPosition) % 16);
    if (this->enableRequestMargin && requestPosition > SEEK_MARGIN_SIZE) {
      requestPosition = (offsetPosition - SEEK_MARGIN_SIZE) -
                        ((offsetPosition - SEEK_MARGIN_SIZE) % 16);
      this->enableRequestMargin = false;
    }

    size_t readCapacity = 0;

    try {
      this->httpConnection->get(
          cdnUrl,
          {bell::HTTPClient::RangeHeader::range(
              requestPosition, requestPosition + HTTP_BUFFER_SIZE - 1)});
      requireHttpOk(*this->httpConnection, "audio chunk");  // ADI PATCH
      readCapacity = this->httpConnection->contentLength();

      this->httpConnection->stream().read((char*)this->httpBuffer.data(),
                                          readCapacity);

      if (this->httpConnection->stream().gcount() !=
          (std::streamsize)readCapacity) {
        throw std::runtime_error("Short read of audio chunk body");
      }
    } catch (const std::exception& e) {
      // A network failure must not propagate through the vorbis C call
      // frames above us (TrackPlayer::runTask has no handler). Invalidate
      // the cache window and report EOF so the player survives and moves
      // on to the next track.
      CSPOT_LOG(error, "Failed to read audio chunk at %u: %s",
                (unsigned int)requestPosition, e.what());
      this->lastRequestPosition = 0;
      this->lastRequestCapacity = 0;
      return 0;
    }

    this->lastRequestPosition = requestPosition;
    this->lastRequestCapacity = readCapacity;

    this->decrypt(this->httpBuffer.data(), lastRequestCapacity,

                  this->lastRequestPosition);

    return readBytes(dst, bytes);
  }

  return bytes;
}

size_t CDNAudioFile::getSize() {
  return this->totalFileSize;
}

void CDNAudioFile::decrypt(uint8_t* dst, size_t nbytes, size_t pos) {
  auto calculatedIV = bigNumAdd(audioAESIV, pos / 16);

  this->crypto->aesCTRXcrypt(this->audioKey, calculatedIV, dst, nbytes);
}
