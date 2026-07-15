# Grafana dashboards

Three JSON models live here, all backed by the InfluxDB `xsphere` bucket (Flux):

- `xsphere-dashboard.json` — operator dashboard for the cryostat: temperatures
  (all RTDs + thermocouples), LN2 levels, heater PID, pressure/vacuum, valve
  states, and the slow-control heartbeat.
- `xsphere-omega-dashboard.json` — separate Omega RDXL6SD logger.
- `xsphere-rtd-calibration-dashboard.json` — LabJack RTD calibration compare:
  raw (`source=labjack`) vs corrected (`source=labjack_cal`) resistance and
  temperature, per RTD.  See `slowcontrol/calibration/rtd_calibration.json`
  for the correction coefficients.

It is a starting point — refine panels in the Grafana UI as needed and re-export
over this file (`Dashboard settings → JSON Model`).

## Deploy

Grafana runs in the IOTstack stack on `http://<pi>:3000` (default login
`admin` / `admin`).

1. **Datasource** (once): add an InfluxDB datasource named `InfluxDB-xsphere`,
   UID `influxdb-xsphere`, query language **Flux**, URL `http://influxdb2:8086`
   (the container-network name), organization `xbox-server`, default bucket
   `xsphere`, and an InfluxDB API token with read access to that bucket.

2. **Dashboards**: Dashboards → New → Import → upload each JSON model in this
   directory, and pick the `InfluxDB-xsphere` datasource.

Or provision over the HTTP API — import every dashboard in one go:
```bash
TOKEN=<influxdb token>
curl -s -u admin:admin -H 'Content-Type: application/json' http://<pi>:3000/api/datasources -d '{
  "name":"InfluxDB-xsphere","uid":"influxdb-xsphere","type":"influxdb","access":"proxy",
  "url":"http://influxdb2:8086","isDefault":true,
  "jsonData":{"version":"Flux","organization":"xbox-server","defaultBucket":"xsphere","httpMode":"POST"},
  "secureJsonData":{"token":"'"$TOKEN"'"}}'
for f in xsphere-dashboard.json xsphere-omega-dashboard.json xsphere-rtd-calibration-dashboard.json; do
  curl -s -u admin:admin -H 'Content-Type: application/json' http://<pi>:3000/api/dashboards/db \
    -d "{\"dashboard\":$(sed 's/\${DS_INFLUX}/influxdb-xsphere/g' "$f"),\"overwrite\":true}"
done
```
