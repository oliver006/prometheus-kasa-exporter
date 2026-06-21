# prometheus-kasa-exporter

Prometheus exporter for TP-Link Kasa devices.
Supports plugs (e.g. EP25) and lights

## Local

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python3 kasa_exporter.py --username "$KASA_USERNAME" --password "$KASA_PASSWORD"
```

Scrape one target:

```sh
curl 'http://localhost:9233/scrape?target=kasa-plug-office'
```

Device metrics use `target` and `device` labels. `device="parent"` is the
scraped device itself; power strip and multi-outlet children use their stable
child device ID. Human-readable names, models, firmware, hardware version, MAC,
and region are exposed only on `_info` metrics.

The exporter follows Prometheus naming conventions: time values are seconds,
energy is joules, voltage is volts, current is amperes, power is watts, and
percentage-like values are exported as ratios from 0 to 1.

Device scrape metrics include, when supported by the device:

- State, scrape health, device metadata, firmware metadata, clock timestamp,
  clock offset, RSSI, signal level, cloud connectivity, LED state, child lock,
  overheat and overload state, auto-off settings, and power protection settings.
- Energy-monitoring plug data: instantaneous power, voltage, current, energy
  today, energy this month, energy since reboot, and runtime today/this month.
- Light and dimmer data: brightness, color temperature, HSV components, active
  light effect, active light preset, light strip length, smooth transition
  settings, dimmer fade/gentle/ramp settings.
- Motion and ambient-light data: PIR enabled/triggered/range, PIR threshold,
  PIR values, PIR ADC readings, ambient-light enabled state, and ambient-light
  level.

Exporter self-metrics:

```sh
curl 'http://localhost:9233/metrics'
```

Credentials can be passed as CLI flags or environment variables:

- `--username` / `KASA_USERNAME` / `KASA_USER`
- `--password` / `KASA_PASSWORD` / `KASA_PWD`

## Docker

```sh
docker build -t prometheus-kasa-exporter .
docker run --rm -p 9233:9233 \
  -e KASA_USERNAME="$KASA_USERNAME" \
  -e KASA_PASSWORD="$KASA_PASSWORD" \
  prometheus-kasa-exporter
```

To publish an image that works on both Intel/AMD64 and Apple
Silicon/ARM64 hosts, use Buildx. A plain `docker build` followed by
`docker push` only pushes the architecture of the machine that built it.

```sh
IMAGE=your-registry/prometheus-kasa-exporter:latest
docker buildx inspect prometheus-kasa-multiarch >/dev/null 2>&1 \
  || docker buildx create --name prometheus-kasa-multiarch --driver docker-container --use
docker buildx use prometheus-kasa-multiarch
docker buildx inspect --bootstrap
docker buildx build --platform linux/amd64,linux/arm64 -t "$IMAGE" --push .
docker buildx imagetools inspect "$IMAGE"
```

The same multi-arch push is available through make:

```sh
make docker-push-multiarch IMAGE=your-registry/prometheus-kasa-exporter:latest
make docker-inspect IMAGE=your-registry/prometheus-kasa-exporter:latest
```

The container must be able to resolve and reach the Kasa targets. On Linux
Docker hosts, `--network host` can be useful for LAN device access.

Prometheus multi-target scrape example:

```yaml
scrape_configs:
  - job_name: kasa
    metrics_path: /scrape
    static_configs:
      - targets:
          - kasa-plug-1
          - kasa-plug-2
          - kasa-lights-1
    relabel_configs:
      - source_labels: [__address__]
        target_label: __param_target
      - source_labels: [__param_target]
        target_label: instance
      - target_label: __address__
        replacement: kasa-exporter:9233
```
