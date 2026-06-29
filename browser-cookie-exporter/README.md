# Adobe Cookie Exporter

A small Chrome or Edge extension used to export Adobe or Firefly cookies in the
minimal JSON format required by `adobe2api`.

## Export Format

```json
{
  "cookie": "k1=v1; k2=v2",
  "headers": {
    "x-arp-session-id": "base64-json-value"
  }
}
```

The `headers.x-arp-session-id` field is exported only when the active tab is a
loaded `https://firefly.adobe.com/` page with Firefly session data available.

## Install

1. Open `chrome://extensions` or `edge://extensions`
2. Enable developer mode
3. Click `Load unpacked`
4. Select the `browser-cookie-exporter/` folder

## Usage

1. Log in to Adobe or Firefly and open `https://firefly.adobe.com/generate/image`
2. Open the extension popup
3. Choose an export scope:
   - `Adobe domains (recommended)`
   - `Current site`
4. Click `Export Minimal JSON`

## Import Into adobe2api

```bash
curl -X POST "http://127.0.0.1:6001/api/v1/refresh-profiles/import-cookie" \
  -H "Content-Type: application/json" \
  -d '{"name":"my-account","cookie":"k1=v1; k2=v2"}'
```

## Incognito Support

The extension exports cookies from the cookie store used by the active tab.
If you open the popup from an incognito Adobe or Firefly tab, the exported JSON
will contain the incognito cookie jar instead of the regular browser cookie jar.

To use it in incognito:

1. Open `chrome://extensions` or `edge://extensions`
2. Open this extension's details page
3. Enable `Allow in Incognito`
4. Open Adobe or Firefly in an incognito window
5. Open the popup from that incognito tab and export the JSON
