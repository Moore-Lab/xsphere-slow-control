# Grafana dashboard

`xsphere-dashboard.json` is the operator dashboard for the xsphere slow-control
system — temperatures (all RTDs + thermocouples), LN2 levels, heater PID,
pressure/vacuum, valve states, and the slow-control heartbeat. It queries the
InfluxDB `xsphere` bucket (Flux).

It is a starting point — refine panels in the Grafana UI as needed and re-export
over this file (`Dashboard settings → JSON Model`).

## Deploy

Grafana runs in the IOTstack stack on `http://<pi>:3000` (default login
`admin` / `admin`).

1. **Datasource** (once): add an InfluxDB datasource named `InfluxDB-xsphere`,
   UID `influxdb-xsphere`, query language **Flux**, URL `http://influxdb2:8086`
   (the container-network name), organization `xbox-server`, default bucket
   `xsphere`, and an InfluxDB API token with read access to that bucket.

2. **Dashboard**: Dashboards → New → Import → upload `xsphere-dashboard.json`,
   and pick the `InfluxDB-xsphere` datasource.

Or provision both over the HTTP API:
```bash
TOKEN=<influxdb token>
curl -s -u admin:admin -H 'Content-Type: application/json' http://<pi>:3000/api/datasources -d '{
  "name":"InfluxDB-xsphere","uid":"influxdb-xsphere","type":"influxdb","access":"proxy",
  "url":"http://influxdb2:8086","isDefault":true,
  "jsonData":{"version":"Flux","organization":"xbox-server","defaultBucket":"xsphere","httpMode":"POST"},
  "secureJsonData":{"token":"'"$TOKEN"'"}}'
curl -s -u admin:admin -H 'Content-Type: application/json' http://<pi>:3000/api/dashboards/db -d "{\"dashboard\":$(sed 's/\${DS_INFLUX}/influxdb-xsphere/g' xsphere-dashboard.json),\"overwrite\":true}"
```
