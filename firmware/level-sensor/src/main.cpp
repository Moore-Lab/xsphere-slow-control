/*
 * xsphere LN2 level sensor firmware (FDC1004).
 *
 * Reads one channel of the ProtoCentral FDC1004 capacitance sensor and
 * publishes the raw capacitance (pF) to MQTT so the Python autovalve
 * controller can filter and threshold it.
 *
 * Hardware:
 *   ESP32 dev module
 *   ProtoCentral FDC1004 breakout (I2C, default address 0x50)
 *   Coaxial capacitance probe submerged in LN2 dewar
 *
 * VERIFY items before flashing:
 *   - WIFI_SSID / WIFI_PASSWORD
 *   - MQTT_BROKER IP address
 *   - FDC1004_CHANNEL: which channel the probe is wired to (see platformio.ini)
 *   - CAPDAC_OFFSET_PF: subtract this from the raw pF reading to zero the
 *     sensor with an empty vessel.  Start at 0; calibrate on first use.
 *   - Re-calibrate level thresholds in slowcontrol/config.yaml after
 *     determining the pF-per-cm relationship for your specific probe geometry.
 *
 * MQTT topics:
 *   xsphere/sensors/level/{VESSEL_NAME}
 *       {"raw_pf": <float>, "vessel": "<name>", "valid": true|false}
 *
 *   xsphere/status/level_{VESSEL_NAME}
 *       {"uptime_s": <int>, "rssi": <int>, "ip": "<str>"}
 *
 * Build configuration:
 *   VESSEL_NAME, MQTT_CLIENT_ID, and FDC1004_CHANNEL are set per-environment
 *   in platformio.ini so the same source compiles for all vessels.
 */

#include <Arduino.h>
#include <Wire.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <Protocentral_FDC1004.h>

// ──────────────────────────────────────────────────────────────────────────────
// Configuration — VERIFY BEFORE FLASHING
// ──────────────────────────────────────────────────────────────────────────────

static const char* WIFI_SSID     = "xbox-radio";       // VERIFY
static const char* WIFI_PASSWORD = "levitatingxenon";  // VERIFY
static const char* MQTT_BROKER   = "192.168.8.116";    // xbox-pi  VERIFY
static const int   MQTT_PORT     = 1883;

// VESSEL_NAME and MQTT_CLIENT_ID are injected by platformio.ini build_flags
#ifndef VESSEL_NAME
#  define VESSEL_NAME "unknown"
#endif
#ifndef MQTT_CLIENT_ID
#  define MQTT_CLIENT_ID "xsphere-level-unknown"
#endif

// FDC1004 channel (0–3) — set in platformio.ini per vessel
#ifndef FDC1004_CHANNEL
#  define FDC1004_CHANNEL FDC1004_CHANNEL_0
#endif

// Offset subtracted from raw reading to zero the sensor with an empty vessel.
// Calibrate on first use; set to 0 until then.
static const float CAPDAC_OFFSET_PF = 0.0f;   // CALIBRATE

// Publish interval (ms)
static const unsigned long LEVEL_INTERVAL_MS  = 2000;
static const unsigned long STATUS_INTERVAL_MS = 30000;

// ──────────────────────────────────────────────────────────────────────────────

FDC1004 fdc(FDC1004_RATE_100HZ);

WiFiClient   wifiClient;
PubSubClient mqttClient(wifiClient);

static unsigned long lastLevel  = 0;
static unsigned long lastStatus = 0;
static unsigned long startTime  = 0;

// ──────────────────────────────────────────────────────────────────────────────
// Helpers
// ──────────────────────────────────────────────────────────────────────────────

void mqttReconnect() {
    unsigned long t0 = millis();
    while (!mqttClient.connected() && millis() - t0 < 10000) {
        Serial.printf("[MQTT] connecting as %s ...", MQTT_CLIENT_ID);
        if (mqttClient.connect(MQTT_CLIENT_ID)) {
            Serial.println(" OK");
        } else {
            Serial.printf(" failed rc=%d — retry\n", mqttClient.state());
            delay(2000);
        }
    }
}

void publishJson(const char* topic, JsonDocument& doc) {
    char buf[256];
    serializeJson(doc, buf, sizeof(buf));
    mqttClient.publish(topic, buf, false);
}

// ──────────────────────────────────────────────────────────────────────────────
// Setup
// ──────────────────────────────────────────────────────────────────────────────

void setup() {
    Serial.begin(115200);
    delay(500);
    Serial.printf("\n=== xsphere level sensor: %s ===\n", VESSEL_NAME);

    // WiFi
    Serial.printf("[WiFi] connecting to %s ...", WIFI_SSID);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    while (WiFi.status() != WL_CONNECTED) {
        delay(500);
        Serial.print(".");
    }
    Serial.printf("\n[WiFi] connected, IP: %s\n", WiFi.localIP().toString().c_str());

    // MQTT
    mqttClient.setServer(MQTT_BROKER, MQTT_PORT);
    mqttReconnect();

    // I2C + FDC1004
    Wire.begin();
    if (!fdc.begin()) {
        Serial.println("[FDC1004] INIT FAILED — check wiring");
        // Continue anyway; sensor will report invalid readings
    } else {
        Serial.println("[FDC1004] OK");
    }

    startTime = millis();
    Serial.printf("[level-sensor/%s] ready\n", VESSEL_NAME);
}

// ──────────────────────────────────────────────────────────────────────────────
// Loop
// ──────────────────────────────────────────────────────────────────────────────

void loop() {
    if (!mqttClient.connected()) mqttReconnect();
    mqttClient.loop();

    unsigned long now = millis();

    // ── Level measurement ─────────────────────────────────────────────────
    if (now - lastLevel >= LEVEL_INTERVAL_MS) {
        lastLevel = now;

        char topic[64];
        snprintf(topic, sizeof(topic), "xsphere/sensors/level/%s", VESSEL_NAME);

        fdc1004_capacitance_t meas = fdc.getCapacitanceMeasurement(
            static_cast<fdc1004_channel_t>(FDC1004_CHANNEL)
        );

        JsonDocument doc;
        doc["vessel"] = VESSEL_NAME;

        if (!isnan(meas.capacitance_pf)) {
            float adjusted = meas.capacitance_pf - CAPDAC_OFFSET_PF;
            doc["raw_pf"] = roundf(adjusted * 1000.0f) / 1000.0f;
            doc["valid"]  = true;
            if (meas.capdac_out_of_range) {
                doc["capdac_warning"] = true;  // CAPDAC adjusting; reading may be transient
            }
        } else {
            doc["raw_pf"] = nullptr;
            doc["valid"]  = false;
        }

        publishJson(topic, doc);
    }

    // ── Status ───────────────────────────────────────────────────────────
    if (now - lastStatus >= STATUS_INTERVAL_MS) {
        lastStatus = now;

        char topic[64];
        snprintf(topic, sizeof(topic), "xsphere/status/level_%s", VESSEL_NAME);

        JsonDocument doc;
        doc["uptime_s"] = (now - startTime) / 1000;
        doc["rssi"]     = WiFi.RSSI();
        doc["ip"]       = WiFi.localIP().toString();
        doc["vessel"]   = VESSEL_NAME;
        publishJson(topic, doc);
    }
}
