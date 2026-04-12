/*
 * xsphere Gas Handling System (GHS) ESP32 sensor firmware.
 *
 * Hardware (verify before flashing):
 *   ESP32 dev module
 *   ADS1115 #1  I2C addr 0x48 (ADDR pin → GND)
 *   ADS1115 #2  I2C addr 0x49 (ADDR pin → VCC)
 *   DHT11       GPIO 4
 *   BMP3XX      I2C (default address)
 *
 * Channel assignments:
 *   ADC1 CH0  → Pressure gauge 0  (0–25 PSI,  0–10 V output)
 *   ADC1 CH1  → Pressure gauge 1  (0–100 PSI, 0–10 V output)
 *   ADC1 CH2  → Vacuum gauge 0    (Pirani, 0–10 V logarithmic)
 *   ADC1 CH3  → Vacuum gauge 1    (Pirani, 0–10 V logarithmic)
 *   ADC2 CH0  → (unconnected — former ballast level, now separate board)
 *   ADC2 CH1  → (unconnected — former primary_xe level, now separate board)
 *
 * Voltage divider scaling:
 *   Sensors output 0–10 V; ADS1115 is set to ±2.048 V FSR.
 *   A resistor divider (R_series / R_shunt ratio = FEG ≈ 5.003) scales
 *   the 0–10 V signal down to 0–2.0 V before the ADC.
 *   Measured voltage * FEG = actual sensor output voltage.
 *
 * MQTT topics (new xsphere schema):
 *   xsphere/sensors/pressure/main      {"value_psi": X, "voltage_v": X}
 *   xsphere/sensors/pressure/high      {"value_psi": X, "voltage_v": X}
 *   xsphere/sensors/vacuum/xe_cube     {"value_mbar": X, "voltage_v": X}
 *   xsphere/sensors/vacuum/pump        {"value_mbar": X, "voltage_v": X}
 *   xsphere/sensors/environment/lab    {"temperature_c": X, "humidity_pct": X}
 *   xsphere/sensors/environment/bmp    {"temperature_c": X, "pressure_hpa": X}
 *   xsphere/status/ghs_esp32           {"uptime_s": X, "rssi": X, "ip": "..."}
 *
 * VERIFY items:
 *   - WiFi SSID / password
 *   - MQTT broker IP
 *   - FEG voltage divider constant matches actual resistors
 *   - Pressure conversion coefficients (PSI per volt)
 *   - Vacuum conversion formula (Pirani gauge model)
 *   - DHT11 GPIO pin
 */

#include <Arduino.h>
#include <Wire.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <DHT.h>
#include <Adafruit_BMP3XX.h>
#include "Ads1115.h"

// ──────────────────────────────────────────────────────────────────────────────
// Configuration — VERIFY ALL VALUES BEFORE FLASHING
// ──────────────────────────────────────────────────────────────────────────────

static const char* WIFI_SSID       = "xbox-radio";          // VERIFY
static const char* WIFI_PASSWORD   = "levitatingxenon";     // VERIFY
static const char* MQTT_BROKER     = "192.168.8.116";       // xbox-pi IP
static const int   MQTT_PORT       = 1883;
static const char* MQTT_CLIENT_ID  = "xsphere-ghs-esp32";

static const int   DHT_PIN         = 4;                     // VERIFY
static const float FEG             = 180.1f / 36.0f;        // VERIFY: voltage divider ratio

// Pressure conversion coefficients (voltage → engineering units)
static const float PSI_PER_VOLT_MAIN = 2.5f;   // 10 V → 25 PSI  VERIFY
static const float PSI_PER_VOLT_HIGH = 10.0f;  // 10 V → 100 PSI VERIFY

// Vacuum conversion (Pfeiffer/Edwards Pirani logarithmic): p [mbar] = 10^(1.667 * V - 11.33)
// VERIFY against your specific vacuum gauge model and documentation
static const float VAC_A = 1.667f;
static const float VAC_B = 11.33f;

// How often to read all sensors and publish (ms)
static const unsigned long PUBLISH_INTERVAL_MS = 5000;

// Status publish interval (ms)
static const unsigned long STATUS_INTERVAL_MS = 30000;

// ──────────────────────────────────────────────────────────────────────────────

#define I2C_ADDR_ADC1  0x48   // ADS1115 #1: ADDR→GND
#define I2C_ADDR_ADC2  0x49   // ADS1115 #2: ADDR→VCC

Ads1115 Adc1(I2C_ADDR_ADC1);
Ads1115 Adc2(I2C_ADDR_ADC2);
DHT dht(DHT_PIN, DHT11);
Adafruit_BMP3XX bmp;

WiFiClient   wifiClient;
PubSubClient mqttClient(wifiClient);

static unsigned long lastPublish = 0;
static unsigned long lastStatus  = 0;
static unsigned long startTime   = 0;

// ──────────────────────────────────────────────────────────────────────────────
// Helpers
// ──────────────────────────────────────────────────────────────────────────────

float readChannel(Ads1115& adc, uint8_t ch) {
    adc.SetSingleCh(ch);
    adc.StartSingleConv();
    while (adc.IsBusy()) {}
    return adc.GetResultVolt() * FEG;
}

void mqttReconnect() {
    unsigned long start = millis();
    while (!mqttClient.connected() && millis() - start < 10000) {
        Serial.print("[MQTT] connecting...");
        if (mqttClient.connect(MQTT_CLIENT_ID)) {
            Serial.println(" connected");
        } else {
            Serial.printf(" failed rc=%d — retrying in 2s\n", mqttClient.state());
            delay(2000);
        }
    }
}

void publishJson(const char* topic, JsonDocument& doc) {
    char buf[256];
    size_t n = serializeJson(doc, buf, sizeof(buf));
    if (n >= sizeof(buf)) {
        Serial.printf("[MQTT] payload truncated for topic: %s\n", topic);
    }
    mqttClient.publish(topic, buf, false);
}

// ──────────────────────────────────────────────────────────────────────────────
// Setup
// ──────────────────────────────────────────────────────────────────────────────

void setup() {
    Serial.begin(115200);
    delay(500);
    Serial.println("\n=== xsphere GHS ESP32 firmware ===");

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

    // I2C
    Wire.begin();

    // ADS1115 — set ±2.048 V FSR (matches 0–2 V input from divider)
    if (!Adc1.Init()) Serial.println("[ADS1115] ADC1 init FAILED");
    else {
        Adc1.SetFullScaleRange(ADS1115_PGA_2048);
        Serial.println("[ADS1115] ADC1 OK");
    }
    if (!Adc2.Init()) Serial.println("[ADS1115] ADC2 init FAILED");
    else {
        Adc2.SetFullScaleRange(ADS1115_PGA_2048);
        Serial.println("[ADS1115] ADC2 OK");
    }

    // DHT11
    dht.begin();
    Serial.println("[DHT11] initialized");

    // BMP3XX
    if (!bmp.begin_I2C()) {
        Serial.println("[BMP3XX] init FAILED — sensor absent or wiring issue");
    } else {
        bmp.setTemperatureOversampling(BMP3_OVERSAMPLING_8X);
        bmp.setPressureOversampling(BMP3_OVERSAMPLING_4X);
        bmp.setIIRFilterCoeff(BMP3_IIR_FILTER_COEFF_3);
        bmp.setOutputDataRate(BMP3_ODR_50_HZ);
        Serial.println("[BMP3XX] OK");
    }

    startTime = millis();
    Serial.println("[GHS] setup complete");
}

// ──────────────────────────────────────────────────────────────────────────────
// Loop
// ──────────────────────────────────────────────────────────────────────────────

void loop() {
    // Maintain MQTT
    if (!mqttClient.connected()) mqttReconnect();
    mqttClient.loop();

    unsigned long now = millis();

    // ── Sensor publish cycle ───────────────────────────────────────────────
    if (now - lastPublish >= PUBLISH_INTERVAL_MS) {
        lastPublish = now;

        // ADC1 — Pressure channels
        float v0 = readChannel(Adc1, 0);  // CH0: 0–25 PSI
        float v1 = readChannel(Adc1, 1);  // CH1: 0–100 PSI
        float v2 = readChannel(Adc1, 2);  // CH2: Pirani vacuum
        float v3 = readChannel(Adc1, 3);  // CH3: Pirani vacuum

        // Pressure (PSI)
        float psi_main = v0 * PSI_PER_VOLT_MAIN;
        float psi_high = v1 * PSI_PER_VOLT_HIGH;

        // Vacuum (mbar) — Pirani logarithmic
        float mbar_0 = powf(10.0f, VAC_A * v2 - VAC_B);
        float mbar_1 = powf(10.0f, VAC_A * v3 - VAC_B);

        // Publish pressure/main
        {
            JsonDocument doc;
            doc["value_psi"] = roundf(psi_main * 1000.0f) / 1000.0f;
            doc["voltage_v"] = roundf(v0 * 1000.0f) / 1000.0f;
            publishJson("xsphere/sensors/pressure/main", doc);
        }

        // Publish pressure/high
        {
            JsonDocument doc;
            doc["value_psi"] = roundf(psi_high * 1000.0f) / 1000.0f;
            doc["voltage_v"] = roundf(v1 * 1000.0f) / 1000.0f;
            publishJson("xsphere/sensors/pressure/high", doc);
        }

        // Publish vacuum/xe_cube
        {
            JsonDocument doc;
            doc["value_mbar"] = mbar_0;
            doc["voltage_v"]  = roundf(v2 * 1000.0f) / 1000.0f;
            publishJson("xsphere/sensors/vacuum/xe_cube", doc);
        }

        // Publish vacuum/pump
        {
            JsonDocument doc;
            doc["value_mbar"] = mbar_1;
            doc["voltage_v"]  = roundf(v3 * 1000.0f) / 1000.0f;
            publishJson("xsphere/sensors/vacuum/pump", doc);
        }

        // DHT11 — lab environment
        float humidity = dht.readHumidity();
        float temp_c   = dht.readTemperature();
        if (!isnan(humidity) && !isnan(temp_c)) {
            JsonDocument doc;
            doc["temperature_c"] = roundf(temp_c   * 100.0f) / 100.0f;
            doc["humidity_pct"]  = roundf(humidity  * 100.0f) / 100.0f;
            publishJson("xsphere/sensors/environment/lab", doc);
        } else {
            Serial.println("[DHT11] read failed");
        }

        // BMP3XX — barometric pressure + board temp
        if (bmp.performReading()) {
            JsonDocument doc;
            doc["temperature_c"] = roundf(bmp.temperature * 100.0f) / 100.0f;
            doc["pressure_hpa"]  = roundf((bmp.pressure / 100.0f) * 10.0f) / 10.0f;
            publishJson("xsphere/sensors/environment/bmp", doc);
        }
    }

    // ── Status publish ─────────────────────────────────────────────────────
    if (now - lastStatus >= STATUS_INTERVAL_MS) {
        lastStatus = now;
        JsonDocument doc;
        doc["uptime_s"] = (now - startTime) / 1000;
        doc["rssi"]     = WiFi.RSSI();
        doc["ip"]       = WiFi.localIP().toString();
        publishJson("xsphere/status/ghs_esp32", doc);
    }
}
