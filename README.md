# Entegra Dig File Generator

Streamlit app for Entegra dig sheets. Upload the dig sheet PDFs, the assessment summary, the
pipeline KMZ, your staking report template and a cheat sheet template. Get one filled staking
report per dig sheet PDF (one per replacement, "REP n") and one cheat sheet, each report with
driving directions and an aerial image sized to the template's image slot.

Your staking report template holds your crew's names and phone numbers, so it is **not** in this
repo: upload it each time you run the app.

## Where each report field comes from

| Field | Source |
|---|---|
| Dig name | the whole heading at the top of the dig sheet, followed by `ODO <odometer>` |
| Line name | the part of the dig sheet's file name after the first underscore (`533_8 ARCHER-WF_REP 2_...` -> `8 ARCHER-WF`) |
| Tool type | the `Assessment:` line of the assessment summary |
| ODO | "Odometer Distance of Target Feature" |
| U/S and D/S reference | the reference names on the dig sheet, with each reference's printed station beneath it |
| Feet to each AGM | target ODO minus / plus the reference's odometer; goes in E5 and K5 (the ONEOK layout puts it under the reference instead) |
| Stationing | worked out from the U/S and D/S reference stations (see below) |
| County, state | reverse geocoded from the dig's GPS coordinates (FCC, then Census, then Mapbox) |
| Directions | Mapbox, the same logic as the Dig Site Directions Generator |
| Aerial image | KMZ centerline, AGMs and the REP's span / welds / tie-ins on satellite imagery |
| Alignment sheet, tract, legal description, HCA | `Unknown` unless typed into the review table |
| Staking notes | left exactly as the template has them |

Never written: the phone number (the template's lookup keyed off *Surveyed By*), survey date,
latitude, longitude, elevation and EDOC (field measurements).

## One dig per PDF

A dig sheet PDF is one replacement, named by its REP number (`..._REP_6_...`). Digs are labelled
"REP 6" on the aerial and in the cheat sheet. If a PDF holds more than one anomaly (REP 6 holds
two), the first by odometer is the dig; the others are listed with Include unticked.

## Dig sheets that are missing their own page

Some dig sheet PDFs hold the neighbouring joints' sheets but not the sheet for the target
feature itself. Those digs are still produced: odometer comes from the dig name, and the weld
distances and coordinates are worked out from the joint tables on the sheets that are there
(each joint's U/S weld odometer and latitude/longitude). The review table flags them.

## Stationing

The dig sheet prints stations only for the two references. Stationing and odometer are
different scales and are not expected to agree, so the default interpolates between the two
reference stations in proportion to the odometer. The sidebar can switch to "U/S reference
station + odometer offset".

## Cells are found by their labels

The staking report is filled by looking for "Upstream Reference", "Alignment Sheet:" and so
on, not by fixed addresses. A cell whose label is not found is left alone and reported.

## Run

    pip install -r requirements.txt
    cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # add a Mapbox token
    streamlit run app.py

The Mapbox token enables directions and the Mapbox satellite basemap. Without it the aerial
uses Esri World Imagery and directions are left blank.

## Layout

    app.py        the whole app, deliberately one file (no local imports to break on Streamlit Cloud)
    templates/    the cheat sheet templates. Put your staking template here too if you like,
                  but do not commit it to a public repo: it holds contact details.
    tests/        pytest

## Tests

    pip install pytest
    pytest

The parser tests that need real dig sheet PDFs skip themselves when the files are absent.
