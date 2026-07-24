#include "AccessKeyFetcher.h"

#include <cstring>           // for strrchr
#include <exception>         // ADI PATCH
#include <string>            // ADI PATCH
#include <string_view>       // ADI PATCH
#include <initializer_list>  // for initializer_list
#include <map>               // for operator!=, operator==
#include <type_traits>       // for remove_extent_t
#include <vector>            // for vector

#include "BellLogger.h"    // for AbstractLogger
#include "BellUtils.h"     // for BELL_SLEEP_MS
#include "CSpotContext.h"  // for Context
#include "HTTPClient.h"
#include "Logger.h"            // for CSPOT_LOG
#include "MercurySession.h"    // for MercurySession, MercurySession::Res...
#include "NanoPBExtensions.h"  // for bell::nanopb::encode...
#include "NanoPBHelper.h"      // for pbEncode and pbDecode
#include "Packet.h"            // for cspot
#include "TimeProvider.h"      // for TimeProvider
#include "Utils.h"             // for string_format

#ifdef BELL_ONLY_CJSON
#include "cJSON.h"
#else
#include "nlohmann/json.hpp"      // for basic_json<>::object_t, basic_json
#include "nlohmann/json_fwd.hpp"  // for json
#endif

using namespace cspot;

static std::string SCOPES =
    "streaming,user-library-read,user-library-modify,user-top-read,user-read-"
    "recently-played";  // Required access scopes

AccessKeyFetcher::AccessKeyFetcher(std::shared_ptr<cspot::Context> ctx)
    : ctx(ctx) {}

bool AccessKeyFetcher::isExpired() {
  if (accessKey.empty()) {
    return true;
  }

  if (ctx->timeProvider->getSyncedTimestamp() > expiresAt) {
    return true;
  }

  return false;
}

std::string AccessKeyFetcher::getAccessKey() {
  if (!isExpired()) {
    return accessKey;
  }

  updateAccessKey();

  return accessKey;
}

void AccessKeyFetcher::updateAccessKey() {
  if (keyPending) {
    // Already pending refresh request
    return;
  }

  keyPending = true;

  // Max retry of 3, can receive different hash cat types
  int retryCount = 3;
  bool success = false;

  do {
    CSPOT_LOG(info, "Access token expired, fetching new one...");

    // ADI PATCH: every failure used to be fatal or silent. A network/TLS exception escaped
    // into a task with no handler (abort() -> reboot) and, even if caught further up, left
    // keyPending stuck at true so the token was never refreshed again; an unexpected reply
    // dereferenced a null cJSON pointer. Now each failure is logged with its reason and
    // simply counts as a failed attempt.
    try {
      auto credentials =
          "grant_type=client_credentials&client_id=" + ctx->config.clientId +
          "&client_secret=" + ctx->config.clientSecret;
      std::vector<uint8_t> body(credentials.begin(), credentials.end());

      auto response = bell::HTTPClient::post(
          "https://accounts.spotify.com/api/token",
          {{"Content-Type", "application/x-www-form-urlencoded"}}, body);

      const int status = response->statusCode;
      std::string newKey;
      int expiresIn = 0;
      std::string problem;

#ifdef BELL_ONLY_CJSON
      std::string_view raw = response->body();
      cJSON* root =
          raw.empty() ? nullptr : cJSON_ParseWithLength(raw.data(), raw.size());
      cJSON* token = root ? cJSON_GetObjectItem(root, "access_token") : nullptr;
      cJSON* expires = root ? cJSON_GetObjectItem(root, "expires_in") : nullptr;
      if (cJSON_IsString(token) && token->valuestring != nullptr &&
          cJSON_IsNumber(expires)) {
        newKey = token->valuestring;
        expiresIn = expires->valueint;
      } else if (root == nullptr) {
        problem = raw.empty() ? "empty response body" : "invalid JSON";
      } else {
        cJSON* err = cJSON_GetObjectItem(root, "error");
        cJSON* desc = cJSON_GetObjectItem(root, "error_description");
        problem = std::string("error=") +
                  (cJSON_IsString(err) ? err->valuestring : "none") +
                  ", description=" +
                  (cJSON_IsString(desc) ? desc->valuestring : "none");
      }
      if (root != nullptr) {
        cJSON_Delete(root);
      }
#else
      auto root = nlohmann::json::parse(response->bytes());
      if (root.contains("access_token") && root.contains("expires_in")) {
        newKey = std::string(root["access_token"]);
        expiresIn = root["expires_in"];
      } else {
        problem = root.contains("error") ? std::string(root["error"])
                                         : "no access_token in response";
      }
#endif

      if (!newKey.empty()) {
        accessKey = newKey;
        // Successfully received an auth token
        CSPOT_LOG(info, "Access token sucessfully fetched");
        success = true;

        this->expiresAt =
            ctx->timeProvider->getSyncedTimestamp() + (expiresIn * 1000);
      } else {
        CSPOT_LOG(error, "Failed to fetch access token (HTTP %d): %s", status,
                  problem.c_str());
        BELL_SLEEP_MS(3000);
      }
    } catch (const std::exception& e) {
      CSPOT_LOG(error, "Failed to fetch access token: %s", e.what());
      BELL_SLEEP_MS(3000);
    } catch (...) {
      CSPOT_LOG(error, "Failed to fetch access token: unknown exception");
      BELL_SLEEP_MS(3000);
    }

    retryCount--;
  } while (retryCount >= 0 && !success);

  keyPending = false;
}
