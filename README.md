# Relabel Waybill Generator (web UI)

Static page (GitHub Pages) for generating a waybill from the Cross Border Relabel Tool. It talks **only**
to a local agent running on the user's own computer (`http://127.0.0.1:8765`); the agent does the browser
automation and returns the generated ZPL and the rendered waybill. No data is sent anywhere else by this
page, and no credentials or captured payloads pass through it.

The page needs the local agent; without it, it shows how to start it.
