"""Entegra Dig File Generator - single file Streamlit app.

Upload the Entegra dig sheets (PDF), the assessment summary (for the tool type),
the pipeline KMZ, a staking report template and the dig stake cheat sheet
template(s). Get back one filled staking report per dig and one filled cheat
sheet, with driving directions and a correctly sized aerial image in every report.

What each report field is built from
------------------------------------
Dig Name            the full name printed at the top of the dig sheet
Line name           the text after the first underscore of the dig sheet's file name
Tool type           the "Assessment:" line of the assessment summary
ODO                 "Odometer Distance of Target Feature" on the dig sheet
Upstream / D/S ref  the reference names on the dig sheet; feet = target ODO minus
                    the reference's odometer
County / State      reverse geocoded from the dig's GPS coordinates
Aerial image        the KMZ centerline and AGMs, drawn on satellite imagery
Directions          Mapbox, exactly as in the other generators
Staking notes       left exactly as the template has them
Alignment sheet, tract, legal description, HCA
                    "Unknown" unless filled in on the review table

A wrong value in a signed report is worse than a missing one, so anything the
uploads cannot establish is left "Unknown" or blank and flagged.

Deliberately kept as one file with no local package imports: Streamlit Cloud
deployments break silently when a subpackage does not make it into the repo.
"""

from __future__ import annotations

import datetime as _dt
import io
import math
import os
import re
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
from copy import copy
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Optional
from xml.etree import ElementTree
from xml.sax.saxutils import escape

import requests
from PIL import Image, ImageDraw, ImageFont


# ==========================================================================
# MODELS
# ==========================================================================

UNKNOWN = "Unknown"

# Reference markers the dig sheet names: "AGM 9", "MLV 12", "8-22 Anchor Rd".
REFERENCE_NAME_RE = re.compile(r"^\s*(AGM|MLV|MLB|BV)\s*0*(\d+)", re.I)


def reference_name(reference) -> Optional[str]:
    """'AGM 9' -> 'AGM 9'. Whitespace is collapsed; the marker is otherwise as printed."""
    if reference is None:
        return None
    text = re.sub(r"\s+", " ", str(reference)).strip()
    if not text:
        return None
    match = re.match(r"^(AGM|MLV|MLB|BV)\s+(\d+)", text, re.I)
    if match:
        return f"{match.group(1).upper()} {match.group(2)}"
    return text


def _number(value) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _round2(value):
    return None if value is None else round(float(value), 2)


@dataclass
class Joint:
    """One pipe joint from a dig sheet's joint table."""

    number: str
    length: Optional[float]
    odometer: Optional[float]      # odometer of the joint's U/S weld
    latitude: Optional[float]      # position of that same U/S weld
    longitude: Optional[float]


@dataclass
class Dig:
    """One dig, with everything the reports need."""

    # --- identity --------------------------------------------------------
    name: str = ""                 # the whole heading: 533_Archer to WF_Dig 2_2026_306_52314.98_5569_None
    label: str = ""                # "Dig 2"
    pipeline_id: str = ""          # "533"
    segment_name: str = ""         # "Archer to WF" (the free-format part of the heading)
    ili_range: str = ""            # "306"
    year: str = ""
    feature_id: str = ""           # target FID, "5569"
    source_file: str = ""
    line_name: str = ""            # report J2: from the dig sheet's file name
    tool_type: str = ""            # report J3: from the assessment

    # --- from the dig sheet ----------------------------------------------
    odometer: Optional[float] = None       # odometer distance of the target feature
    us_ref: str = ""
    us_ref_odometer: Optional[float] = None
    us_ref_station: str = ""               # as printed; not used (see station note)
    ds_ref: str = ""
    ds_ref_odometer: Optional[float] = None
    ds_ref_station: str = ""
    us_weld_distance: Optional[float] = None   # feet, target feature to its U/S weld
    ds_weld_distance: Optional[float] = None   # feet, target feature to its D/S weld
    us_weld_odometer: Optional[float] = None
    target_joint: str = ""
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    feature_type: str = ""
    # True when the target feature's own sheet is not in the PDF and its values were
    # worked out from the other sheets' joint tables.
    derived: bool = False
    station_method: str = "interpolate"    # or "offset"; see station_feet
    # A dig sheet PDF is one replacement (REP). When it holds several digs, only the first
    # (lowest odometer) is primary and is included by default.
    primary: bool = True
    heading_label: str = ""        # "Dig 4" - the number inside the PDF, before REP relabelling

    # --- filled in by the app / by hand ----------------------------------
    county: str = UNKNOWN
    state: str = UNKNOWN
    alignment_sheet: str = UNKNOWN
    tract_number: str = UNKNOWN
    legal_description: str = UNKNOWN
    hca: str = UNKNOWN
    directions: str = ""
    aerial_image: Optional[bytes] = None
    notes: str = ""
    warnings: list = field(default_factory=list)

    # --- convenience -----------------------------------------------------
    @property
    def upstream_reference(self) -> str:
        return reference_name(self.us_ref) or ""

    @property
    def downstream_reference(self) -> str:
        return reference_name(self.ds_ref) or ""

    @property
    def us_ref_distance(self) -> Optional[float]:
        """Feet from the U/S reference to the target feature."""
        if self.odometer is None or self.us_ref_odometer is None:
            return None
        return self.odometer - self.us_ref_odometer

    @property
    def ds_ref_distance(self) -> Optional[float]:
        """Feet from the target feature to the D/S reference."""
        if self.odometer is None or self.ds_ref_odometer is None:
            return None
        return self.ds_ref_odometer - self.odometer

    @property
    def nearest_reference_label(self) -> str:
        """The cheat sheet's 'Feet from AGM U/S-D/S' cell, e.g. "AGM 9 1609.37'"."""
        candidates = []
        if self.us_ref_distance is not None and self.upstream_reference:
            candidates.append((self.us_ref_distance, self.upstream_reference))
        if self.ds_ref_distance is not None and self.downstream_reference:
            candidates.append((self.ds_ref_distance, self.downstream_reference))
        if not candidates:
            return ""
        distance, label = min(candidates, key=lambda pair: pair[0])
        return f"{label} {distance:.2f}'"

    @staticmethod
    def _station_feet(text: str) -> Optional[float]:
        """'1660+25.7' -> 166025.7 feet."""
        match = re.match(r"^\s*(\d+)\s*\+\s*(\d+(?:\.\d+)?)\s*$", text or "")
        return int(match.group(1)) * 100 + float(match.group(2)) if match else None

    @property
    def station_feet(self) -> Optional[float]:
        """Station of the target feature, in the dig sheet's own stationing.

        The dig sheet prints stations only for the two references, so the target's is
        worked out from them. Stationing and odometer are different scales that are not
        expected to agree (here they differ by a constant factor), so the default
        interpolates between the two printed stations in proportion to the odometer,
        which stays on the stationing's own scale. ``station_method == "offset"`` adds
        the odometer distance from the U/S reference to its station instead.
        """
        us = self._station_feet(self.us_ref_station)
        if us is None or self.us_ref_distance is None:
            return None
        ds = self._station_feet(self.ds_ref_station)
        span = None
        if self.us_ref_odometer is not None and self.ds_ref_odometer is not None:
            span = self.ds_ref_odometer - self.us_ref_odometer
        if self.station_method == "interpolate" and ds is not None and span:
            return us + self.us_ref_distance / span * (ds - us)
        return us + self.us_ref_distance

    @property
    def station_label(self) -> str:
        feet = self.station_feet
        if feet is None:
            return ""
        hundreds, rest = divmod(round(feet, 1), 100)
        return f"{int(hundreds)}+{rest:04.1f}"

    @property
    def hca_label(self) -> str:
        text = (self.hca or "").strip()
        if not text or text == UNKNOWN:
            return UNKNOWN
        return text.title()

    @property
    def group(self) -> tuple:
        """Digs on the same pipeline and ILI range share an odometer scale."""
        return (self.pipeline_id, self.ili_range)

    @property
    def key(self) -> str:
        return self.name


# ==========================================================================
# XLSMPATCH
# ==========================================================================
# Edit an .xlsm in place at the package level.
#
# openpyxl cannot round-trip this template: it drops DrawingML shapes, which
# would take the macro-linked buttons and the logos with them. So the workbook is
# treated as what it is - a zip of XML parts - and only the bytes that need to
# change are changed. Styles, macros, buttons, print settings, comments and the
# logos all survive untouched.

R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
XDR_NS = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"

EXCEL_EPOCH = _dt.datetime(1899, 12, 30)


@dataclass
class ImageSlot:
    """A picture already anchored in the template, ready to be swapped out.

    ``drawing_path``, ``rel_id`` and the ``pic_*`` offsets are what let the
    aerial be given its own media part instead of overwriting the one already
    in the package. In the Enterprise template the StakingReport image slot and
    the Pre_Dig_Photos placeholders are the *same* media part (image6.jpeg), so
    overwriting its bytes would replace the pre-dig photo placeholders too.
    """

    media_path: str
    extension: str
    width_emu: int
    height_emu: int
    drawing_path: str = ""
    rel_id: str = ""
    pic_start: int = 0
    pic_end: int = 0

    @property
    def aspect(self) -> float:
        if not self.height_emu:
            return 2.0
        return self.width_emu / self.height_emu


class XlsmPatcher:
    def __init__(self, data: bytes):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.names = list(archive.namelist())
            self.parts = {name: archive.read(name) for name in self.names}
            self.infos = {info.filename: info for info in archive.infolist()}
        self._sheet_paths = self._map_sheets()

    # -- structure ------------------------------------------------------
    def _text(self, path: str) -> str:
        return self.parts[path].decode("utf-8")

    def _map_sheets(self) -> dict:
        workbook = self._text("xl/workbook.xml")
        rels = self._text("xl/_rels/workbook.xml.rels")

        targets = {
            match.group(1): match.group(2)
            for match in re.finditer(
                r'<Relationship[^>]*Id="([^"]+)"[^>]*Target="([^"]+)"', rels
            )
        }
        mapping = {}
        for match in re.finditer(r"<sheet\b[^>]*/?>", workbook):
            tag = match.group(0)
            name = re.search(r'name="([^"]*)"', tag)
            rid = re.search(r'r:id="([^"]*)"', tag)
            if not name or not rid:
                continue
            target = targets.get(rid.group(1), "")
            if not target:
                continue
            path = target if target.startswith("xl/") else "xl/" + target.lstrip("/")
            path = path.replace("/./", "/")
            mapping[name.group(1)] = path
        return mapping

    def sheet_names(self) -> list:
        return list(self._sheet_paths)

    # -- cell values ----------------------------------------------------
    def set_values(self, sheet_name: str, values: dict, literal: bool = False) -> list:
        """Write {'C19': value} into a sheet. Returns cells it could not place.

        ``literal=True`` writes text exactly as given. Without it, text that
        starts with "=" becomes a formula - right for the few cells the app
        sets itself, wrong for data copied out of a dig sheet.
        """
        path = self._sheet_paths.get(sheet_name)
        if path is None:
            raise KeyError(f"No sheet named {sheet_name!r} in this workbook")

        xml = self._text(path)
        missing = []
        for ref, value in values.items():
            if value is None:
                continue
            xml, ok = _write_cell(xml, ref, value, literal)
            if not ok:
                missing.append(ref)
        self.parts[path] = xml.encode("utf-8")
        return missing

    def restyle(self, sheet_name: str, refs, size: Optional[float] = None,
                shrink: bool = False) -> None:
        """Give cells a copy of their own style with a smaller font and/or
        shrink-to-fit. The original style is left alone, so other cells that
        share it are unaffected."""
        path = self._sheet_paths.get(sheet_name)
        if path is None:
            return
        xml = self._text(path)
        styles = self._text("xl/styles.xml")

        fonts_match = re.search(r"(<fonts\b[^>]*>)(.*?)(</fonts>)", styles, re.S)
        xfs_match = re.search(r"(<cellXfs\b[^>]*>)(.*?)(</cellXfs>)", styles, re.S)
        if not fonts_match or not xfs_match:
            return
        font_pattern = r"<font\b[^>]*?/>|<font\b[^>]*?>.*?</font>"
        xf_pattern = r"<xf\b[^>]*?/>|<xf\b[^>]*?>.*?</xf>"
        fonts = re.findall(font_pattern, fonts_match.group(2), re.S)
        xfs = re.findall(xf_pattern, xfs_match.group(2), re.S)

        made = {}

        def variant(index: int) -> int:
            if index in made:
                return made[index]
            xf = xfs[index]
            if size is not None:
                font_match = re.search(r'fontId="(\d+)"', xf)
                font_id = int(font_match.group(1)) if font_match else 0
                font = fonts[font_id]
                if "<sz " in font:
                    font = re.sub(r'<sz val="[^"]*"\s*/>', f'<sz val="{size:g}"/>', font, count=1)
                else:
                    font = font.replace("<font>", f'<font><sz val="{size:g}"/>', 1)
                fonts.append(font)
                new_font = len(fonts) - 1
                xf = (re.sub(r'fontId="\d+"', f'fontId="{new_font}"', xf, count=1)
                      if font_match else xf.replace("<xf ", f'<xf fontId="{new_font}" ', 1))
                if "applyFont=" in xf:
                    xf = re.sub(r'applyFont="\d"', 'applyFont="1"', xf, count=1)
                else:
                    xf = xf.replace("<xf ", '<xf applyFont="1" ', 1)
            if shrink:
                if xf.endswith("/>") and "</xf>" not in xf:
                    xf = xf[:-2] + "></xf>"
                if "<alignment" in xf:
                    xf = re.sub(r'\swrapText="\d"', "", xf, count=1)
                    if "shrinkToFit=" not in xf:
                        xf = xf.replace("<alignment", '<alignment shrinkToFit="1"', 1)
                else:
                    xf = re.sub(r"(<xf\b[^>]*?>)", r'\1<alignment shrinkToFit="1"/>', xf, count=1)
                if "applyAlignment=" in xf:
                    xf = re.sub(r'applyAlignment="\d"', 'applyAlignment="1"', xf, count=1)
                else:
                    xf = xf.replace("<xf ", '<xf applyAlignment="1" ', 1)
            xfs.append(xf)
            made[index] = len(xfs) - 1
            return made[index]

        for ref in refs:
            match = re.search(rf'<c r="{ref}"([^>]*?)(/?)>', xml)
            if not match:
                continue
            style = re.search(r'\ss="(\d+)"', match.group(1))
            index = int(style.group(1)) if style else 0
            if index >= len(xfs):
                continue
            attrs = match.group(1)
            new_index = variant(index)
            attrs = (re.sub(r'\ss="\d+"', f' s="{new_index}"', attrs, count=1)
                     if style else attrs + f' s="{new_index}"')
            xml = xml[:match.start()] + f'<c r="{ref}"{attrs}{match.group(2)}>' + xml[match.end():]

        if not made:
            return
        fonts_head = re.sub(r'count="\d+"', f'count="{len(fonts)}"', fonts_match.group(1), count=1)
        xfs_head = re.sub(r'count="\d+"', f'count="{len(xfs)}"', xfs_match.group(1), count=1)
        # Replace the later section first so the earlier match offsets stay valid.
        first, second = sorted(
            [(fonts_match, fonts_head, "".join(fonts)), (xfs_match, xfs_head, "".join(xfs))],
            key=lambda item: item[0].start(), reverse=True)
        for match, head, body in (first, second):
            styles = styles[:match.start()] + head + body + match.group(3) + styles[match.end():]
        self.parts["xl/styles.xml"] = styles.encode("utf-8")
        self.parts[path] = xml.encode("utf-8")

    def cell_text(self, sheet_name: str, ref: str) -> str:
        """The text a cell shows: an inline or shared string, or a formula's last result."""
        path = self._sheet_paths.get(sheet_name)
        if path is None:
            return ""
        match = _cell_pattern(ref).search(self._text(path))
        if not match or not match.group("body"):
            return ""
        attrs, body = match.group("attrs"), match.group("body")
        kind = re.search(r'\st="(\w+)"', attrs)
        kind = kind.group(1) if kind else ""
        if kind == "inlineStr":
            return _unescape("".join(re.findall(r"<t[^>]*>(.*?)</t>", body, re.S)))
        value = re.search(r"<v>(.*?)</v>", body, re.S)
        if value is None:
            return ""
        if kind == "s" and "xl/sharedStrings.xml" in self.parts:
            items = re.findall(r"<si>(.*?)</si>", self._text("xl/sharedStrings.xml"), re.S)
            index = int(value.group(1))
            if index < len(items):
                return _unescape("".join(re.findall(r"<t[^>]*>(.*?)</t>", items[index], re.S)))
            return ""
        return _unescape(value.group(1))

    def box_size(self, sheet_name: str, ref: str) -> Optional[tuple]:
        """(width in pixels, height in points) of the merged box a cell starts."""
        path = self._sheet_paths.get(sheet_name)
        if path is None:
            return None
        xml = self._text(path)
        merge = re.search(rf'<mergeCell ref="{ref}:([A-Z]+)(\d+)"', xml)
        start_row = int(re.search(r"\d+", ref).group(0))
        first_col = _column_index(re.match(r"[A-Z]+", ref).group(0))
        if merge:
            last_col, last_row = _column_index(merge.group(1)), int(merge.group(2))
        else:
            last_col, last_row = first_col, start_row

        default_width = re.search(r'<sheetFormatPr[^>]*defaultColWidth="([\d.]+)"', xml)
        default_width = float(default_width.group(1)) if default_width else 8.43
        default_height = re.search(r'<sheetFormatPr[^>]*defaultRowHeight="([\d.]+)"', xml)
        default_height = float(default_height.group(1)) if default_height else 15.0
        widths = {}
        for col in re.finditer(r"<col\b[^>]*/>", xml):
            tag = col.group(0)
            low, high = re.search(r'min="(\d+)"', tag), re.search(r'max="(\d+)"', tag)
            width = re.search(r'width="([\d.]+)"', tag)
            if low and high and width:
                for index in range(int(low.group(1)), min(int(high.group(1)), 200) + 1):
                    widths[index] = float(width.group(1))
        heights = {}
        for row in re.finditer(r"<row\b[^>]*>", xml):
            tag = row.group(0)
            number, height = re.search(r' r="(\d+)"', tag), re.search(r' ht="([\d.]+)"', tag)
            if number and height:
                heights[int(number.group(1))] = float(height.group(1))
        width_px = sum(widths.get(i, default_width) * 7 for i in range(first_col, last_col + 1))
        height_pt = sum(heights.get(r, default_height) for r in range(start_row, last_row + 1))
        return width_px, height_pt

    def set_cached(self, sheet_name: str, values: dict) -> None:
        """Refresh the value a formula cell last computed, leaving the formula.

        The template's formulas carry whatever Excel last computed (zeros, in a
        blank template). Excel recalculates on open, but anything that previews
        the file without calculating - a phone, a mail client, SharePoint's
        viewer - would show those stale zeros. Writing the right cached value
        beside each formula makes the file correct everywhere.
        """
        path = self._sheet_paths.get(sheet_name)
        if path is None:
            return
        xml = self._text(path)
        for ref, value in values.items():
            xml = _set_cached_value(xml, ref, value)
        self.parts[path] = xml.encode("utf-8")

    # -- images ---------------------------------------------------------
    def find_image_slot(self, sheet_name: str) -> Optional[ImageSlot]:
        """The largest picture anchored on a sheet - the aerial image slot."""
        drawing = self._drawing_for(sheet_name)
        if drawing is None:
            return None
        drawing_xml = self._text(drawing)
        rels_path = _rels_path(drawing)
        if rels_path not in self.parts:
            return None
        rels = self._text(rels_path)
        targets = {
            match.group(1): match.group(2)
            for match in re.finditer(
                r'<Relationship[^>]*Id="([^"]+)"[^>]*Target="([^"]+)"', rels
            )
        }

        best = None
        for block in re.finditer(r"<xdr:pic\b.*?</xdr:pic>", drawing_xml, re.S):
            chunk = block.group(0)
            embed = re.search(r'r:embed="([^"]+)"', chunk)
            extent = re.search(r'<a:ext\s+cx="(\d+)"\s+cy="(\d+)"', chunk)
            if not embed or not extent:
                continue
            width, height = int(extent.group(1)), int(extent.group(2))
            target = targets.get(embed.group(1))
            if not target:
                continue
            media = _resolve(drawing, target)
            if media not in self.parts:
                continue
            if best is None or width * height > best.width_emu * best.height_emu:
                best = ImageSlot(
                    media_path=media,
                    extension=media.rsplit(".", 1)[-1].lower(),
                    width_emu=width,
                    height_emu=height,
                    drawing_path=drawing,
                    rel_id=embed.group(1),
                    pic_start=block.start(),
                    pic_end=block.end(),
                )
        return best

    def replace_image(self, slot: ImageSlot, image_bytes: bytes,
                      extension: str = "png") -> None:
        """Point this one picture at a new media part.

        The obvious implementation - overwrite ``slot.media_path`` - is wrong
        for this template. Its StakingReport image slot and the Pre_Dig_Photos
        placeholders share a single media part, so overwriting the bytes put
        the aerial in place of the pre-dig photo placeholders as well. Those
        are filled in by hand after the survey, so they have to be left alone.

        Instead the aerial is added as a new part and only the StakingReport
        picture's own relationship is repointed at it. Every other picture,
        on this sheet or any other, keeps the media it already had.
        """
        if not slot.drawing_path or not slot.rel_id:
            # Nothing to repoint - fall back to the in-place swap.
            self.parts[slot.media_path] = image_bytes
            return

        extension = extension.lower().lstrip(".")
        media_path = self._new_media_path(extension)
        self.parts[media_path] = image_bytes
        self._ensure_content_type(extension)

        rels_path = _rels_path(slot.drawing_path)
        rels = self._text(rels_path)
        new_id = self._new_rel_id(rels)
        target = _relative_part(media_path, slot.drawing_path)
        entry = (
            f'<Relationship Id="{new_id}" Type="http://schemas.openxmlformats.org/'
            f'officeDocument/2006/relationships/image" Target="{target}"/>'
        )
        self.parts[rels_path] = rels.replace(
            "</Relationships>", entry + "</Relationships>", 1
        ).encode("utf-8")

        drawing_xml = self._text(slot.drawing_path)
        chunk = drawing_xml[slot.pic_start:slot.pic_end]
        chunk = chunk.replace(f'r:embed="{slot.rel_id}"', f'r:embed="{new_id}"', 1)
        self.parts[slot.drawing_path] = (
            drawing_xml[:slot.pic_start] + chunk + drawing_xml[slot.pic_end:]
        ).encode("utf-8")

    def _new_media_path(self, extension: str) -> str:
        index = 1
        while f"xl/media/image{index}.{extension}" in self.parts:
            index += 1
        while any(
            name.startswith(f"xl/media/image{index}.") for name in self.parts
        ):
            index += 1
        return f"xl/media/image{index}.{extension}"

    @staticmethod
    def _new_rel_id(rels: str) -> str:
        used = {int(n) for n in re.findall(r'Id="rId(\d+)"', rels)}
        candidate = max(used) + 1 if used else 1
        return f"rId{candidate}"

    def _ensure_content_type(self, extension: str) -> None:
        path = "[Content_Types].xml"
        if path not in self.parts:
            return
        xml = self._text(path)
        if f'Extension="{extension}"' in xml:
            return
        kind = "image/jpeg" if extension in ("jpg", "jpeg") else f"image/{extension}"
        entry = f'<Default Extension="{extension}" ContentType="{kind}"/>'
        self.parts[path] = xml.replace("<Types", "<Types", 1).replace(
            "</Types>", entry + "</Types>", 1
        ).encode("utf-8")

    def _drawing_for(self, sheet_name: str) -> Optional[str]:
        path = self._sheet_paths.get(sheet_name)
        if path is None:
            return None
        rels_path = _rels_path(path)
        if rels_path not in self.parts:
            return None
        rels = self._text(rels_path)
        match = re.search(
            r'<Relationship[^>]*Type="[^"]*/drawing"[^>]*Target="([^"]+)"', rels
        )
        if not match:
            match = re.search(
                r'<Relationship[^>]*Target="([^"]+)"[^>]*Type="[^"]*/drawing"', rels
            )
        if not match:
            return None
        return _resolve(path, match.group(1))

    def force_full_recalc(self) -> None:
        """Make Excel recalculate every formula when the file is opened.

        Formula cells carry the value Excel last computed. The template's
        phone number is an XLOOKUP against the surveyor name with a stale
        cached result, so without this the report opens showing the old value
        until something nudges the cell.
        """
        path = "xl/workbook.xml"
        if path not in self.parts:
            return
        xml = self._text(path)
        match = re.search(r"<calcPr\b[^>]*/?>", xml)
        if match:
            tag = match.group(0)
            if "fullCalcOnLoad" in tag:
                return
            updated = tag.rstrip("/>").rstrip() + ' fullCalcOnLoad="1"/>'
            xml = xml[: match.start()] + updated + xml[match.end():]
        else:
            xml = xml.replace(
                "</workbook>", '<calcPr fullCalcOnLoad="1"/></workbook>'
            )
        self.parts[path] = xml.encode("utf-8")

    # -- output ---------------------------------------------------------
    def to_bytes(self) -> bytes:
        buffer = io.BytesIO()
        # Parts added since the template was opened (the aerial's own media
        # part) are not in self.names, so they are appended at the end.
        added = [name for name in self.parts if name not in self.infos]
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in self.names + added:
                info = self.infos.get(name)
                if info is None:
                    new = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                else:
                    new = zipfile.ZipInfo(name, date_time=info.date_time)
                    new.external_attr = info.external_attr
                new.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(new, self.parts[name])
        return buffer.getvalue()


# ---------------------------------------------------------------------------
# Cell writing
# ---------------------------------------------------------------------------

def _cell_pattern(ref: str) -> re.Pattern:
    return re.compile(
        rf'<c r="{ref}"(?P<attrs>[^>]*?)(?:/>|>(?P<body>.*?)</c>)', re.S
    )


def _style_of(attrs: str) -> str:
    match = re.search(r'\ss="(\d+)"', attrs or "")
    return f' s="{match.group(1)}"' if match else ""


_ERROR_VALUES = {"#DIV/0!", "#N/A", "#NAME?", "#NULL!", "#NUM!", "#REF!", "#VALUE!"}


def _excel_time(value: _dt.time) -> float:
    return (value.hour * 3600 + value.minute * 60 + value.second
            + value.microsecond / 1e6) / 86400.0


def _render(ref: str, style: str, value, literal: bool = False) -> str:
    if isinstance(value, bool):
        return f'<c r="{ref}"{style} t="b"><v>{1 if value else 0}</v></c>'
    if isinstance(value, (int, float)):
        return f'<c r="{ref}"{style}><v>{value!r}</v></c>'
    if isinstance(value, _dt.datetime):
        serial = (value - EXCEL_EPOCH).total_seconds() / 86400.0
        return f'<c r="{ref}"{style}><v>{serial!r}</v></c>'
    if isinstance(value, _dt.date):
        serial = (_dt.datetime(value.year, value.month, value.day) - EXCEL_EPOCH).days
        return f'<c r="{ref}"{style}><v>{serial}</v></c>'
    if isinstance(value, _dt.time):
        return f'<c r="{ref}"{style}><v>{_excel_time(value)!r}</v></c>'
    if isinstance(value, _dt.timedelta):
        return f'<c r="{ref}"{style}><v>{value.total_seconds() / 86400.0!r}</v></c>'

    text = str(value)
    if literal and text in _ERROR_VALUES:
        return f'<c r="{ref}"{style} t="e"><v>{text}</v></c>'
    if text.startswith("=") and not literal:
        return f'<c r="{ref}"{style}><f>{escape(text[1:])}</f></c>'
    return (
        f'<c r="{ref}"{style} t="inlineStr">'
        f'<is><t xml:space="preserve">{escape(text)}</t></is></c>'
    )


def _set_cached_value(xml: str, ref: str, value) -> str:
    """Replace the <v> of a formula cell, keeping its <f> and style."""
    match = _cell_pattern(ref).search(xml)
    if not match or not match.group("body"):
        return xml
    body = match.group("body")
    formula = re.search(r"<f\b[^>]*?(?:/>|>.*?</f>)", body, re.S)
    if not formula:
        return xml
    attrs = re.sub(r'\st="[^"]*"', "", match.group("attrs") or "")

    if value is None or value == "":
        kind, text = ' t="str"', ""
    elif isinstance(value, bool):
        kind, text = ' t="b"', "1" if value else "0"
    elif isinstance(value, (int, float)):
        kind, text = "", repr(value)
    else:
        kind, text = ' t="str"', escape(str(value))
    cell = f'<c r="{ref}"{attrs}{kind}>{formula.group(0)}<v>{text}</v></c>'
    return xml[: match.start()] + cell + xml[match.end():]


def _write_cell(xml: str, ref: str, value, literal: bool = False) -> tuple:
    match = _cell_pattern(ref).search(xml)
    if match:
        replacement = _render(ref, _style_of(match.group("attrs")), value, literal)
        return xml[: match.start()] + replacement + xml[match.end():], True

    inserted = _insert_cell(xml, ref, value, literal)
    return (inserted, True) if inserted is not None else (xml, False)


_COLUMN_RE = re.compile(r"([A-Z]+)(\d+)")


def _column_index(letters: str) -> int:
    index = 0
    for character in letters:
        index = index * 26 + (ord(character) - 64)
    return index


def _insert_cell(xml: str, ref: str, value, literal: bool = False) -> Optional[str]:
    """Add a cell to an existing row, keeping columns in order."""
    parsed = _COLUMN_RE.fullmatch(ref)
    if not parsed:
        return None
    letters, number = parsed.group(1), parsed.group(2)
    target = _column_index(letters)

    row = re.search(rf'<row[^>]*\br="{number}"[^>]*>(.*?)</row>', xml, re.S)
    if not row:
        return None

    body = row.group(1)
    style = ""
    insert_at = len(body)
    for cell in re.finditer(r'<c r="([A-Z]+)\d+"([^>]*?)(?:/>|>.*?</c>)', body, re.S):
        index = _column_index(cell.group(1))
        if index < target:
            style = _style_of(cell.group(2)) or style
        else:
            insert_at = cell.start()
            break

    new_body = body[:insert_at] + _render(ref, style, value, literal) + body[insert_at:]
    return xml[: row.start(1)] + new_body + xml[row.end(1):]


# ---------------------------------------------------------------------------
# Package path helpers
# ---------------------------------------------------------------------------

def _rels_path(part: str) -> str:
    folder, _, name = part.rpartition("/")
    return f"{folder}/_rels/{name}.rels"


def _relative_part(part: str, base_part: str) -> str:
    """``part`` expressed relative to the folder holding ``base_part``.

    The inverse of ``_resolve``, used when adding a relationship.
    """
    target = part.split("/")
    folder = base_part.rpartition("/")[0].split("/") if "/" in base_part else []
    shared = 0
    while (shared < len(folder) and shared < len(target) - 1
           and folder[shared] == target[shared]):
        shared += 1
    return "/".join([".."] * (len(folder) - shared) + target[shared:])


def _resolve(base_part: str, target: str) -> str:
    folder = base_part.rpartition("/")[0]
    parts = folder.split("/") if folder else []
    for chunk in target.split("/"):
        if chunk in ("", "."):
            continue
        if chunk == "..":
            if parts:
                parts.pop()
        else:
            parts.append(chunk)
    return "/".join(parts)





# ==========================================================================
# DIGSHEET
# ==========================================================================
# Read Entegra dig sheets out of PDF.
#
# What the real files look like (checked against the 7 Archer-to-Wichita PDFs):
#
#   * A page is a letter-size sheet with ONE dig sheet, or a 960x540 slide with
#     up to three sheets across and two down. Every sheet has exactly one
#     "Item Number NNNN" line, and the sheet's own "U/S Reference" label marks
#     its left edge, so a page is cut into one region per sheet before reading.
#   * The dig's name is printed once, as the heading of the first page of its
#     group: 533_Archer to WF_Dig 15_2026_306_57911.31_6809_None. The sheets that
#     follow - on that page or the next ones, which carry no heading of their
#     own - belong to it, until the next heading.
#   * The last two tokens of that name are the target feature's odometer and FID.
#     A dig sheet's own "Item Number" is its FID, so the sheet whose Item Number
#     equals the heading's FID is the target's sheet.
#   * A dig that covers several anomalies (Dig 15: FIDs 6805, 6810, 6811) can be
#     missing the target's own sheet - 6809 is not in the PDF. Everything the
#     report needs is then worked out from the joint tables that ARE there; see
#     ``derive_from_joints``.
#
# The text in these PDFs is clean (cells do not overlap), so words are read with
# their positions and matched on the printed labels.

FLOAT_RE = re.compile(r"^-?\d+(?:\.\d+)?$")
STATION_RE = re.compile(r"^\d+\+\d+(?:\.\d+)?$")
TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")

HEADING_RE = re.compile(
    r"^(?P<pipeline>\d+)_(?P<middle>.+)_(?P<year>\d{4})_(?P<range>\d+)_"
    r"(?P<odo>\d+(?:\.\d+)?)_(?P<fid>\d+)_(?P<condition>[A-Za-z0-9\-]+)$"
)

JOINTS_PER_SHEET = 7          # 3rd U/S ... Target ... 3rd D/S
TARGET_COLUMN = 3
ODOMETER_TOLERANCE = 0.06     # feet: sheets of one dig are rounded independently


class DigSheetError(ValueError):
    pass


@dataclass
class SheetData:
    """One dig sheet (one anomaly) as printed."""

    item: str = ""
    us_ref: str = ""
    ds_ref: str = ""
    us_ref_station: str = ""
    ds_ref_station: str = ""
    us_ref_odometer: Optional[float] = None
    ds_ref_odometer: Optional[float] = None
    us_weld_odometer: Optional[float] = None       # U/S weld of the target joint
    joints: list = field(default_factory=list)     # JOINTS_PER_SHEET Joint, odometers filled in
    us_weld_distance: Optional[float] = None
    ds_weld_distance: Optional[float] = None
    odometer: Optional[float] = None               # of the target feature
    feature_type: str = ""
    latitude: Optional[float] = None
    longitude: Optional[float] = None


@dataclass
class DigGroup:
    heading: str
    parts: dict
    source: str
    sheets: list = field(default_factory=list)

    @property
    def middle_parts(self) -> tuple:
        segment, _, label = self.parts["middle"].rpartition("_")
        return segment.strip(), label.strip()

    @property
    def is_dig(self) -> bool:
        return self.middle_parts[1].lower().startswith("dig")


@dataclass
class ParsedPdf:
    groups: list = field(default_factory=list)
    tool_type: str = ""
    assessment: str = ""
    problems: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Words and lines
# ---------------------------------------------------------------------------

def _cluster_lines(words, tolerance: float = 3.0) -> list:
    """Words -> rows (each sorted left to right)."""
    rows = []
    for word in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if rows and abs(word["top"] - rows[-1][0]) <= tolerance:
            rows[-1][1].append(word)
        else:
            rows.append([word["top"], [word]])
    return [sorted(row, key=lambda w: w["x0"]) for _, row in rows]


def _line_text(line) -> str:
    return " ".join(word["text"] for word in line)


def _squash(text: str) -> str:
    """Text with all whitespace removed, so a label matches however its letters are spaced.

    Some sheets (the 3-across slides) set their labels in tiny type that comes
    out of the PDF as "L a t itu d e ( ° )".
    """
    return re.sub(r"\s+", "", text or "")


def _decimals(text: str, places: Optional[int] = None, expect: Optional[int] = None) -> list:
    """Decimal numbers in a piece of text, glued to a label or letter-spaced or not.

    ``places`` is how many decimals the value prints with and ``expect`` how many
    values the row holds. When the numbers come out of the PDF one digit at a time
    ("3 3 . 7 5 2 3 8 9 7 6") the plain read finds the wrong count, and the
    spaces are removed and the digits re-split by ``places``.
    """
    plain_pattern = rf"-?\d+\.\d{{{places}}}(?!\d)" if places else r"-?\d+\.\d+"
    plain = re.findall(plain_pattern, text)
    if expect is None or len(plain) == expect:
        return [float(value) for value in plain]
    squashed_pattern = rf"-?\d+\.\d{{{places}}}" if places else r"-?\d+\.\d+"
    squashed = re.findall(squashed_pattern, _squash(text))
    return [float(value) for value in (squashed if len(squashed) == expect else plain)]


def _joint_numbers(text: str, expect: int) -> list:
    plain = re.findall(r"\b\d+\b", text)
    if len(plain) == expect:
        return plain
    digits = re.sub(r"\D", "", _squash(text))
    if digits and len(digits) % expect == 0:
        size = len(digits) // expect
        return [digits[i * size:(i + 1) * size] for i in range(expect)]
    return plain


def _read_pdf_pages(data: bytes) -> list:
    import pdfplumber

    pages = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            pages.append({
                "width": float(page.width),
                "height": float(page.height),
                "words": page.extract_words(),
            })
    return pages


def find_heading(words) -> Optional[tuple]:
    """The dig name printed across the top of a page, or None.

    The heading shares its line with a red note ("Expose and NDE -10 to 4' from
    the target uGW 13720"), so the line is read word by word and stops at the
    first prefix that is a complete name.
    """
    top = [w for w in words if w["top"] < 30]
    if not top:
        return None
    first_row = _cluster_lines(top)[0]
    text = ""
    for word in first_row:
        text = f"{text} {word['text']}".strip()
        match = HEADING_RE.match(text)
        if match:
            return text, match.groupdict()
    return None


def _item_anchors(words) -> list:
    anchors = []
    for index, word in enumerate(words[:-2]):
        if word["text"] == "Item" and words[index + 1]["text"] == "Number":
            anchors.append({"x0": word["x0"], "top": word["top"], "item": words[index + 2]["text"]})
    return anchors


def _sheet_left_edge(words, anchor) -> float:
    """x of the 'U/S Reference' label that starts the sheet this anchor belongs to."""
    best = None
    for index, word in enumerate(words[:-1]):
        if word["text"] == "U/S" and words[index + 1]["text"] == "Reference":
            if (anchor["top"] < word["top"] < anchor["top"] + 40
                    and word["x0"] <= anchor["x0"]):
                if best is None or word["x0"] > best:
                    best = word["x0"]
    return best if best is not None else anchor["x0"] - 120.0


def split_sheets(page) -> list:
    """[(item, words_in_that_sheet)] for every dig sheet on a page."""
    words = page["words"]
    anchors = _item_anchors(words)
    if not anchors:
        return []
    if len(anchors) == 1:
        return [(anchors[0]["item"], words)]

    for anchor in anchors:
        anchor["left"] = _sheet_left_edge(words, anchor)
    sheets = []
    for anchor in anchors:
        same_row = [a for a in anchors
                    if abs(a["top"] - anchor["top"]) < 20 and a["left"] > anchor["left"] + 50]
        right = min((a["left"] for a in same_row), default=page["width"] + 10) - 8
        below = [a["top"] for a in anchors
                 if a["top"] > anchor["top"] + 60 and abs(a["left"] - anchor["left"]) < 60]
        # The sheet's own "HF Sinclair" banner sits about 26 pt above its Item Number.
        bottom = (min(below) - 26.5) if below else page["height"] + 10
        top = anchor["top"] - 30
        left = anchor["left"] - 8
        sheets.append((anchor["item"], [
            w for w in words
            if left <= w["x0"] < right and top <= w["top"] < bottom
        ]))
    return sheets


# ---------------------------------------------------------------------------
# One sheet
# ---------------------------------------------------------------------------

def _cluster_by_gap(words, gap: float = 25.0) -> list:
    clusters, previous = [], None
    for word in sorted(words, key=lambda w: w["x0"]):
        if previous is not None and word["x0"] - previous["x1"] <= gap:
            clusters[-1].append(word)
        else:
            clusters.append([word])
        previous = word
    return [" ".join(w["text"] for w in cluster) for cluster in clusters]


def _find_pair(line, first: str, second: str, last: bool = False) -> Optional[int]:
    hits = [i for i in range(len(line) - 1)
            if line[i]["text"] == first and line[i + 1]["text"] == second]
    if not hits:
        return None
    return hits[-1] if last else hits[0]


def parse_sheet(item: str, words) -> SheetData:
    lines = _cluster_lines(words)
    sheet = SheetData(item=item)

    def starting(prefix: str):
        wanted = _squash(prefix)
        for line in lines:
            if _squash(_line_text(line)).startswith(wanted):
                return line
        return None

    def containing(*needles: str):
        wanted = [_squash(n) for n in needles]
        for index, line in enumerate(lines):
            text = _squash(_line_text(line))
            if all(n in text for n in wanted):
                return index, line
        return None, None

    # --- references -------------------------------------------------------
    for line in lines:
        start = _find_pair(line, "U/S", "Reference")
        end = _find_pair(line, "D/S", "Reference", last=True)
        if start == 0 and end is not None and end > start:
            names = _cluster_by_gap(line[start + 2:end])
            if len(names) == 2:
                sheet.us_ref, sheet.ds_ref = names
            break
    line = starting("Sta. U/S Ref.")
    if line:
        stations = [w["text"] for w in line if STATION_RE.match(w["text"])]
        if len(stations) == 2:
            sheet.us_ref_station, sheet.ds_ref_station = stations
    line = starting("Odo Dist.")
    if line:
        values = _decimals(_line_text(line), 2, 2)
        if len(values) == 2:
            sheet.us_ref_odometer, sheet.ds_ref_odometer = values
    line = starting("Odometer Distance of U/S Weld of Target Joint")
    if line and _decimals(_line_text(line), 2, 1):
        sheet.us_weld_odometer = _decimals(_line_text(line), 2, 1)[-1]

    # --- joint table ------------------------------------------------------
    def row_text(prefix: str) -> str:
        row = starting(prefix)
        return _line_text(row) if row else ""

    lengths = _decimals(row_text("Joint Length"), 2, JOINTS_PER_SHEET)
    numbers = _joint_numbers(row_text("Joint Number"), JOINTS_PER_SHEET)
    lats = _decimals(row_text("Latitude(\u00b0):"), 8, JOINTS_PER_SHEET)
    lons = _decimals(row_text("Longitude(\u00b0):"), 8, JOINTS_PER_SHEET)
    if not (len(lengths) == len(numbers) == len(lats) == len(lons) == JOINTS_PER_SHEET):
        raise DigSheetError(
            f"Item {item}: expected a {JOINTS_PER_SHEET}-joint table, found "
            f"{len(lengths)} lengths, {len(numbers)} joint numbers, "
            f"{len(lats)} latitudes, {len(lons)} longitudes."
        )
    sheet.joints = [
        Joint(number=numbers[i], length=lengths[i], odometer=None,
              latitude=lats[i], longitude=lons[i])
        for i in range(JOINTS_PER_SHEET)
    ]
    if sheet.us_weld_odometer is not None:
        odometer = [None] * JOINTS_PER_SHEET
        odometer[TARGET_COLUMN] = sheet.us_weld_odometer
        for i in range(TARGET_COLUMN - 1, -1, -1):
            odometer[i] = odometer[i + 1] - sheet.joints[i].length
        for i in range(TARGET_COLUMN + 1, JOINTS_PER_SHEET):
            odometer[i] = odometer[i - 1] + sheet.joints[i - 1].length
        for joint, value in zip(sheet.joints, odometer):
            joint.odometer = value

    # --- target feature ---------------------------------------------------
    # The compass drawn beside these numbers prints 1..12 as whole numbers; the
    # weld distances always carry decimals, so decimals-only skips the dial.
    _, line = containing("Distance from U/S Weld (ft)", "Distance from D/S Weld (ft)")
    if line is not None:
        values = _decimals(_line_text(line), 2, 2)
        if len(values) == 2:
            sheet.us_weld_distance, sheet.ds_weld_distance = values
    line = starting("Odometer Distance of Target Feature")
    if line and _decimals(_line_text(line), 2, 1):
        sheet.odometer = _decimals(_line_text(line), 2, 1)[-1]

    index, _ = containing("Target Anomaly Details")
    if index is not None and index + 2 < len(lines):
        tokens = [w["text"] for w in lines[index + 2]]
        first = next((i for i, t in enumerate(tokens) if FLOAT_RE.match(t)), None)
        if first:
            sheet.feature_type = " ".join(tokens[:first])
    index, _ = containing("Latitude(\u00b0)", "Longitude(\u00b0)", "Elevation(ft)")
    if index is not None:
        for follower in lines[index + 1:index + 4]:
            values = _decimals(_line_text(follower), 8, 2)
            if len(values) >= 2:
                sheet.latitude, sheet.longitude = values[0], values[1]
                break

    missing = [name for name, value in (
        ("reference names", sheet.us_ref and sheet.ds_ref),
        ("reference odometers", sheet.us_ref_odometer is not None and sheet.ds_ref_odometer is not None),
        ("U/S weld odometer", sheet.us_weld_odometer is not None),
        ("target odometer", sheet.odometer is not None),
        ("weld distances", sheet.us_weld_distance is not None and sheet.ds_weld_distance is not None),
        ("GPS coordinates", sheet.latitude is not None and sheet.longitude is not None),
    ) if not value]
    if missing:
        raise DigSheetError(f"Item {item}: could not read the {', '.join(missing)}.")
    return sheet


# ---------------------------------------------------------------------------
# A whole PDF
# ---------------------------------------------------------------------------

def parse_dig_pdf(data: bytes, filename: str) -> ParsedPdf:
    """Every dig group in a PDF, plus the tool type if it is an assessment summary."""
    result = ParsedPdf()
    current: Optional[DigGroup] = None
    for number, page in enumerate(_read_pdf_pages(data), start=1):
        found = find_heading(page["words"])
        if found:
            current = DigGroup(heading=found[0], parts=found[1], source=filename)
            result.groups.append(current)

        for item, words in split_sheets(page):
            if current is None or not current.is_dig:
                result.problems.append(
                    f"{filename}, page {number}: dig sheet {item} has no dig name above it."
                )
                continue
            try:
                current.sheets.append(parse_sheet(item, words))
            except DigSheetError as error:
                result.problems.append(f"{filename}, page {number}: {error}")
    result.groups = [g for g in result.groups if g.is_dig and g.sheets]
    result.assessment, result.tool_type = read_assessment(data)
    return result


def read_assessment(data: bytes) -> tuple:
    """('MFL/DEF/XYZ, Entegra, 3/25/2026.', 'MFL/DEF/XYZ') from an assessment summary page.

    Read with pypdf, not pdfplumber: the summary's cover page is a rotated
    page, whose text pdfplumber returns letter-reversed.
    """
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        for page in reader.pages[:3]:
            match = re.search(r"Assessment:[ \t]*([^\r\n]+)", page.extract_text() or "")
            if match:
                line = match.group(1).strip()
                return line, line.split(",")[0].strip().rstrip(".")
    except Exception:  # noqa: BLE001 - an unreadable cover page just means no tool type
        pass
    return "", ""


def line_name_from_filename(filename: str) -> str:
    """'533_8 ARCHER-WF_REP 2_2026_...pdf' -> '8 ARCHER-WF': the part after the first underscore.

    It runs up to the replacement / dig number. A file whose spaces were turned
    into underscores (533_8_ARCHER-WF_REP_2_...) reads the same way.
    """
    stem = re.sub(r"\.pdf$", "", os.path.basename(filename or ""), flags=re.I)
    if "_" not in stem:
        return ""
    after = stem.split("_", 1)[1]
    head = re.split(r"[_ ](?:REP|Dig)[_ ]\d+", after, maxsplit=1, flags=re.I)[0]
    return re.sub(r"[_\s]+", " ", head).strip()


# ---------------------------------------------------------------------------
# From sheets to a dig
# ---------------------------------------------------------------------------

def merge_joints(sheets, warnings: list) -> dict:
    """{joint number: Joint} from every sheet of one dig.

    Each sheet lists seven joints around its own target joint, so together the
    sheets of a dig cover a longer stretch of pipe than any one of them.
    """
    merged: dict = {}
    for sheet in sheets:
        for joint in sheet.joints:
            if joint.odometer is None:
                continue
            known = merged.get(joint.number)
            if known is None:
                merged[joint.number] = joint
            elif abs(known.odometer - joint.odometer) > ODOMETER_TOLERANCE:
                warnings.append(
                    f"Joint {joint.number} sits at odometer {known.odometer:.2f} on one "
                    f"sheet and {joint.odometer:.2f} on another."
                )
    return merged


def derive_from_joints(joints: dict, odometer: float) -> Optional[dict]:
    """Where a feature at ``odometer`` is, from the joint table alone.

    Finds the joint it falls in, then reads its distance to each weld and
    interpolates its position along the joint between that joint's U/S weld and
    the next joint's (each joint's latitude/longitude is the position of its
    U/S weld, which is also how the sheets' own GPS lines up).
    """
    ordered = sorted((j for j in joints.values() if j.odometer is not None and j.length),
                     key=lambda j: j.odometer)
    for joint in ordered:
        if joint.odometer - 0.005 <= odometer <= joint.odometer + joint.length + 0.005:
            break
    else:
        return None
    end = joint.odometer + joint.length
    follower = next((j for j in ordered if abs(j.odometer - end) <= ODOMETER_TOLERANCE), None)
    if follower is None:
        return None
    fraction = min(max((odometer - joint.odometer) / joint.length, 0.0), 1.0)
    return {
        "joint": joint,
        "us_weld_distance": odometer - joint.odometer,
        "ds_weld_distance": end - odometer,
        "latitude": joint.latitude + (follower.latitude - joint.latitude) * fraction,
        "longitude": joint.longitude + (follower.longitude - joint.longitude) * fraction,
    }


def dig_from_group(group: DigGroup, tool_type: str = "") -> Dig:
    parts = group.parts
    segment, label = group.middle_parts
    target_fid = parts["fid"]
    heading_odometer = float(parts["odo"])
    dig = Dig(
        name=group.heading, label=label, pipeline_id=parts["pipeline"],
        segment_name=segment, ili_range=parts["range"], year=parts["year"],
        feature_id=target_fid, source_file=group.source,
        line_name=line_name_from_filename(group.source) or segment,
        tool_type=tool_type,
    )

    sheets = group.sheets
    own = [s for s in sheets if s.item == target_fid]
    reference_sheet = own[0] if own else sheets[0]

    # The references are the same on every sheet of one dig.
    for sheet in sheets:
        if (sheet.us_ref, sheet.ds_ref) != (reference_sheet.us_ref, reference_sheet.ds_ref):
            dig.warnings.append(
                f"Item {sheet.item} names different references "
                f"({sheet.us_ref} / {sheet.ds_ref}) than item {reference_sheet.item}."
            )
    dig.us_ref, dig.ds_ref = reference_sheet.us_ref, reference_sheet.ds_ref
    dig.us_ref_odometer = reference_sheet.us_ref_odometer
    dig.ds_ref_odometer = reference_sheet.ds_ref_odometer
    dig.us_ref_station = reference_sheet.us_ref_station
    dig.ds_ref_station = reference_sheet.ds_ref_station

    joints = merge_joints(sheets, dig.warnings)

    if own:
        sheet = own[0]
        dig.odometer = sheet.odometer
        dig.us_weld_distance = sheet.us_weld_distance
        dig.ds_weld_distance = sheet.ds_weld_distance
        dig.us_weld_odometer = sheet.us_weld_odometer
        dig.target_joint = sheet.joints[TARGET_COLUMN].number
        dig.latitude, dig.longitude = sheet.latitude, sheet.longitude
        dig.feature_type = sheet.feature_type
        if abs(sheet.odometer - heading_odometer) > 0.02:
            dig.warnings.append(
                f"The sheet's target odometer {sheet.odometer:.2f} differs from the one "
                f"in the dig name ({heading_odometer:.2f})."
            )
    else:
        found = derive_from_joints(joints, heading_odometer)
        dig.derived = True
        dig.odometer = heading_odometer
        if found is None:
            dig.warnings.append(
                f"There is no sheet for the target feature (FID {target_fid}) and its "
                f"position could not be worked out from the other sheets."
            )
        else:
            joint = found["joint"]
            dig.us_weld_distance = found["us_weld_distance"]
            dig.ds_weld_distance = found["ds_weld_distance"]
            dig.us_weld_odometer = joint.odometer
            dig.target_joint = joint.number
            dig.latitude, dig.longitude = found["latitude"], found["longitude"]
            dig.warnings.append(
                f"The PDF has no sheet for the target feature itself (FID {target_fid}); "
                f"its odometer ({heading_odometer:.2f}) is from the dig name and its weld "
                f"distances and coordinates are worked out from the joint tables on the "
                f"other sheets of this dig (items "
                f"{', '.join(s.item for s in sheets)})."
            )
    return dig


def parse_dig_pdfs(files: list) -> tuple:
    """files = [(file name, bytes)] -> (digs, tool_type, problems)."""
    digs, problems, tool_type, seen = [], [], "", {}
    parsed = []
    for filename, data in files:
        try:
            result = parse_dig_pdf(data, filename)
        except Exception as error:  # noqa: BLE001 - surfaced to the user
            problems.append(f"{filename}: could not be read ({error})")
            continue
        parsed.append((filename, result))
        problems.extend(result.problems)
        if result.tool_type and not tool_type:
            tool_type = result.tool_type
    for filename, result in parsed:
        for group in result.groups:
            dig = dig_from_group(group, tool_type)
            if dig.name in seen:
                problems.append(f"{dig.name} appears in {seen[dig.name]} and {filename}; "
                                f"the first one is used.")
                continue
            seen[dig.name] = filename
            digs.append(dig)
        if not result.groups and not result.tool_type:
            problems.append(f"{filename}: no dig sheets or assessment found.")
    digs.sort(key=lambda d: (d.group, d.odometer or 0.0))

    # One dig sheet PDF is one replacement (REP n), and crews call the dig by that number
    # (the headings inside number the anomalies on their own scale: REP 3 is "Dig 4").
    # Digs are labelled "REP n"; a PDF holding several anomalies keeps the first as the
    # dig and marks the rest as extras that are left out unless ticked.
    by_file: dict = {}
    for dig in digs:
        by_file.setdefault(dig.source_file, []).append(dig)
    for members in by_file.values():
        members.sort(key=lambda d: d.odometer or 0.0)
        for index, dig in enumerate(members):
            rep = rep_number(dig)
            if not rep:
                continue
            dig.heading_label = dig.label
            dig.primary = True            # every anomaly in the PDF is produced
            dig.label = f"REP {rep}" if index == 0 else f"REP {rep} - {dig.heading_label}"
            if index == 0 and len(members) > 1:
                others = ", ".join(f"{m.heading_label} (ODO {m.odometer:.2f})" for m in members[1:])
                dig.warnings.append(
                    f"This PDF holds more than one anomaly: {dig.heading_label} and {others}. "
                    f"Each gets its own report; untick Include on any you don't want.")
    return digs, tool_type, problems




# ==========================================================================
# KMZ
# ==========================================================================
# The KMZ supplies the pipeline centerline, the AGM markers, and each
# replacement's span and weld / tie-in points. Pipeline KMZs run to tens of
# megabytes, so the file is streamed with iterparse and every placemark is
# released as soon as it is read.

KML_DEFAULT_LINE = (255, 255, 255)      # a LineStyle with no <color> is white in KML


def _kml_colour(text: str) -> Optional[tuple]:
    """KML 'aabbggrr' -> (r, g, b)."""
    text = (text or "").strip()
    if len(text) != 8:
        return None
    try:
        blue, green, red = (int(text[i:i + 2], 16) for i in (2, 4, 6))
    except ValueError:
        return None
    return red, green, blue


@dataclass
class KmlLine:
    name: str
    coords: list                       # [(lon, lat), ...]
    colour: tuple = KML_DEFAULT_LINE
    width: float = 2.0
    folder: str = ""
    bbox: tuple = (0.0, 0.0, 0.0, 0.0)  # (min_lon, min_lat, max_lon, max_lat)


@dataclass
class Replacement:
    """One REP's marker points, keyed in the KMZ as 'REP 6 - uGW' and so on."""

    number: str
    points: dict = field(default_factory=dict)     # 'uGW' | 'dGW' | 'uTie_in' | 'dTie_in' -> (lat, lon)
    span: Optional[KmlLine] = None


@dataclass
class KmzData:
    name: str = ""
    lines: list = field(default_factory=list)       # list[KmlLine]
    markers: dict = field(default_factory=dict)     # normalised name -> (label, lat, lon)
    replacements: dict = field(default_factory=dict)  # number -> Replacement

    def marker(self, reference: str) -> Optional[tuple]:
        """(lat, lon) of a reference such as 'AGM 9', or None."""
        label = reference_name(reference)
        if not label:
            return None
        found = self.markers.get(label.lower())
        return (found[1], found[2]) if found else None


class KmzError(ValueError):
    pass


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_coords(text: str) -> list:
    points = []
    for token in (text or "").split():
        parts = token.split(",")
        if len(parts) >= 2:
            try:
                points.append((float(parts[0]), float(parts[1])))
            except ValueError:
                continue
    return points


def _bbox(coords: list) -> tuple:
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    return min(lons), min(lats), max(lons), max(lats)


def parse_kml(stream) -> KmzData:
    """Parse a KML byte stream into lines, named markers and REP data."""
    styles: dict = {}          # id -> (colour or None, width)
    style_maps: dict = {}      # id -> normal style id
    raw_lines: list = []       # (name, coords, style_url, folder)
    raw_points: list = []      # (name, lat, lon, folder)
    path: list = []            # element names from the root
    folders: list = []         # folder names, innermost last
    folder_has_name: list = []
    document_name = ""

    style_id = None
    style_colour = None
    style_width = 2.0
    in_linestyle = False
    map_id = None
    map_key = None

    for event, element in ElementTree.iterparse(stream, events=("start", "end")):
        tag = _local(element.tag)
        if event == "start":
            path.append(tag)
            if tag == "Folder":
                folders.append("")
            elif tag == "Style":
                style_id = element.get("id")
                style_colour, style_width, in_linestyle = None, 2.0, False
            elif tag == "StyleMap":
                map_id = element.get("id")
            elif tag == "LineStyle":
                in_linestyle = True
            continue

        # --- end events ---------------------------------------------------
        parent = path[-2] if len(path) >= 2 else ""
        if tag == "name" and parent == "Folder" and folders:
            folders[-1] = (element.text or "").strip()
        elif tag == "name" and parent == "Document" and not document_name:
            document_name = (element.text or "").strip()
        elif tag == "color" and in_linestyle and parent == "LineStyle":
            style_colour = _kml_colour(element.text)
        elif tag == "width" and in_linestyle and parent == "LineStyle":
            try:
                style_width = float(element.text)
            except (TypeError, ValueError):
                pass
        elif tag == "LineStyle":
            in_linestyle = False
        elif tag == "Style":
            if style_id:
                styles[style_id] = (style_colour, style_width)
            style_id = None
        elif tag == "key" and parent == "Pair":
            map_key = (element.text or "").strip()
        elif tag == "styleUrl" and parent == "Pair" and map_key == "normal" and map_id:
            style_maps[map_id] = (element.text or "").strip().lstrip("#")
        elif tag == "StyleMap":
            map_id, map_key = None, None
        elif tag == "Placemark":
            name_el = element.find("{*}name")
            name = (name_el.text or "").strip() if name_el is not None and name_el.text else ""
            style_el = element.find("{*}styleUrl")
            style_url = (style_el.text or "").strip().lstrip("#") if style_el is not None and style_el.text else ""
            folder = folders[-1] if folders else ""
            for line_el in element.findall(".//{*}LineString"):
                coords_el = line_el.find("{*}coordinates")
                coords = _parse_coords(coords_el.text if coords_el is not None else "")
                if len(coords) >= 2:
                    raw_lines.append((name, coords, style_url, folder))
            point_el = element.find(".//{*}Point")
            if point_el is not None:
                coords_el = point_el.find("{*}coordinates")
                coords = _parse_coords(coords_el.text if coords_el is not None else "")
                if coords:
                    raw_points.append((name, coords[0][1], coords[0][0], folder))
            element.clear()
        elif tag == "Folder":
            folders.pop()
        path.pop()

    def resolve(style_url: str) -> tuple:
        style = styles.get(style_maps.get(style_url, style_url))
        if style is None:
            return KML_DEFAULT_LINE, 2.0
        colour, width = style
        return (colour or KML_DEFAULT_LINE), width

    data = KmzData(name=document_name)
    for name, coords, style_url, folder in raw_lines:
        colour, width = resolve(style_url)
        data.lines.append(KmlLine(name=name, coords=coords, colour=colour, width=width,
                                  folder=folder, bbox=_bbox(coords)))

    rep_point = re.compile(r"^\s*REP\s*(\d+)\s*-\s*(uGW|dGW|uTie_in|dTie_in)\s*$", re.I)
    rep_span = re.compile(r"^\s*REP\s*(\d+)\s*Span\s*$", re.I)
    for name, lat, lon, _folder in raw_points:
        match = rep_point.match(name)
        if match:
            rep = data.replacements.setdefault(match.group(1), Replacement(match.group(1)))
            rep.points[match.group(2)] = (lat, lon)
        elif name:
            data.markers.setdefault(name.lower(), (name, lat, lon))
    for line in data.lines:
        match = rep_span.match(line.name)
        if match:
            rep = data.replacements.setdefault(match.group(1), Replacement(match.group(1)))
            rep.span = line
    return data


def load_kmz(raw: bytes) -> KmzData:
    """Read a .kmz (zip) or a bare .kml."""
    if raw[:2] == b"PK":
        try:
            archive = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile as error:
            raise KmzError(f"The KMZ could not be opened: {error}") from error
        names = [n for n in archive.namelist() if n.lower().endswith(".kml")]
        if not names:
            raise KmzError("The KMZ has no .kml file inside it.")
        preferred = "doc.kml" if "doc.kml" in names else names[0]
        with archive.open(preferred) as stream:
            data = parse_kml(stream)
    else:
        data = parse_kml(io.BytesIO(raw))
    if not data.lines and not data.markers:
        raise KmzError("The KMZ holds no lines or points.")
    return data


def rep_number(dig) -> str:
    """The replacement number in the dig sheet's file name: '..._REP_6_...' -> '6'."""
    match = re.search(r"REP[\s_]*(\d+)", dig.source_file or "", re.I)
    return match.group(1) if match else ""


def _feet_between(lat1, lon1, lat2, lon2) -> float:
    cos = math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot((lat2 - lat1) * 364000.0, (lon2 - lon1) * 364000.0 * cos)


def distance_to_line_feet(coords: list, lat: float, lon: float) -> float:
    """Shortest distance from a point to a polyline, in feet."""
    cos = math.cos(math.radians(lat))
    best = float("inf")
    for (x1, y1), (x2, y2) in zip(coords, coords[1:]):
        ax, ay = (x1 - lon) * 364000.0 * cos, (y1 - lat) * 364000.0
        bx, by = (x2 - lon) * 364000.0 * cos, (y2 - lat) * 364000.0
        vx, vy = bx - ax, by - ay
        length_sq = vx * vx + vy * vy
        t = 0.0 if length_sq == 0 else max(0.0, min(1.0, -(ax * vx + ay * vy) / length_sq))
        best = min(best, math.hypot(ax + t * vx, ay + t * vy))
    return best


def check_dig_against_kmz(dig, kmz: KmzData, tolerance_feet: float = 150.0) -> None:
    """Warn when the dig sheet's coordinates and the KMZ disagree."""
    if dig.latitude is None or dig.longitude is None:
        return
    rep = kmz.replacements.get(rep_number(dig))
    if rep is not None and rep.span is not None:
        feet = distance_to_line_feet(rep.span.coords, dig.latitude, dig.longitude)
        if feet > tolerance_feet:
            dig.warnings.append(
                f"The dig's coordinates are {feet:,.0f} ft from REP {rep.number}'s span in the KMZ."
            )
    elif rep_number(dig):
        dig.warnings.append(f"The KMZ has no span for REP {rep_number(dig)}.")
    for reference in (dig.upstream_reference, dig.downstream_reference):
        if reference and kmz.marker(reference) is None:
            dig.warnings.append(f"{reference} is not a named point in the KMZ.")


# ==========================================================================
# AERIAL
# ==========================================================================
# The aerial image for the staking report: satellite tiles, then the KMZ lines in
# their own colours, this REP's span and weld / tie-in points, the dig's U/S and D/S
# AGMs, a labelled dig pin, a scale bar and a legend. Rendered at the exact aspect
# ratio of the template's image slot so it is never cropped or stretched.

# The StakingReport image slot in the Enterprise template: 6115050 x 3190043 EMU.
# The real aspect is read from whatever template is uploaded; this is the fallback.
SLOT_ASPECT = 6115050 / 3190043

BASEMAPS = {
    # Mapbox uses the same token as the directions generator, and serves 512px
    # retina tiles, so it is the default where a token is configured.
    "Mapbox Satellite": {
        "url": "https://api.mapbox.com/v4/mapbox.satellite/{z}/{x}/{y}@2x.jpg90",
        "tile_size": 512,
        "needs_token": True,
        "max_zoom": 19,
    },
    "Esri World Imagery": {
        "url": (
            "https://server.arcgisonline.com/ArcGIS/rest/services/"
            "World_Imagery/MapServer/tile/{z}/{y}/{x}"
        ),
        "tile_size": 256,
        "needs_token": False,
        "max_zoom": 19,
    },
    "USDA NAIP (USA only)": {
        "url": (
            "https://gis.apfo.usda.gov/arcgis/rest/services/NAIP/"
            "USDA_CONUS_PRIME/ImageServer/tile/{z}/{y}/{x}"
        ),
        "tile_size": 256,
        "needs_token": False,
        "max_zoom": 18,
    },
}


def basemap_options(has_token: bool) -> list:
    return [
        name for name, spec in BASEMAPS.items()
        if has_token or not spec["needs_token"]
    ]

USER_AGENT = "Entegra-Dig-File-Generator/1.0 (staking report automation)"

# Web Mercator maths is done in the tile set's own pixel units, so a retina
# (@2x, 512px) tile set simply doubles the resolution at a given zoom.
DEFAULT_TILE_SIZE = 256

PIPE_RED = (226, 32, 32)
LABEL_WHITE = (255, 255, 255)
SHADOW = (0, 0, 0)


@dataclass
class MapView:
    zoom: int
    centre_lat: float
    centre_lon: float
    width: int
    height: int
    tile_size: int = DEFAULT_TILE_SIZE

    def project(self, longitude: float, latitude: float) -> tuple[float, float]:
        """Longitude/latitude -> pixel coordinates in the rendered image."""
        cx, cy = _lonlat_to_world(self.centre_lon, self.centre_lat, self.zoom, self.tile_size)
        px, py = _lonlat_to_world(longitude, latitude, self.zoom, self.tile_size)
        return px - cx + self.width / 2.0, py - cy + self.height / 2.0


def _lonlat_to_world(longitude, latitude, zoom, tile_size=DEFAULT_TILE_SIZE):
    scale = tile_size * (2 ** zoom)
    x = (longitude + 180.0) / 360.0 * scale
    sin_lat = math.sin(math.radians(max(min(latitude, 85.05112878), -85.05112878)))
    y = (0.5 - math.log((1 + sin_lat) / (1 - sin_lat)) / (4 * math.pi)) * scale
    return x, y


def metres_per_pixel(latitude, zoom, tile_size=DEFAULT_TILE_SIZE) -> float:
    """Ground metres covered by one rendered pixel."""
    base = 156543.03392 * math.cos(math.radians(latitude)) / (2 ** zoom)
    return base * (DEFAULT_TILE_SIZE / tile_size)


def _fetch_tile(session, template: str, zoom: int, x: int, y: int, token: str = ""):
    url = template.format(z=zoom, x=x, y=y)
    if token:
        url += ("&" if "?" in url else "?") + f"access_token={token}"
    try:
        response = session.get(url, timeout=20, headers={"User-Agent": USER_AGENT})
        if response.status_code != 200 or not response.content:
            return None
        return Image.open(io.BytesIO(response.content)).convert("RGB")
    except Exception:
        return None


def _basemap_image(view: MapView, template: str, token: str = "") -> tuple:
    tile = view.tile_size
    cx, cy = _lonlat_to_world(view.centre_lon, view.centre_lat, view.zoom, tile)
    left = cx - view.width / 2.0
    top = cy - view.height / 2.0

    first_x = int(math.floor(left / tile))
    first_y = int(math.floor(top / tile))
    last_x = int(math.floor((left + view.width) / tile))
    last_y = int(math.floor((top + view.height) / tile))

    canvas = Image.new("RGB", (view.width, view.height), (32, 38, 32))
    jobs = [
        (x, y)
        for x in range(first_x, last_x + 1)
        for y in range(first_y, last_y + 1)
    ]
    if not jobs:
        return canvas, 0

    fetched = 0
    max_index = 2 ** view.zoom
    with requests.Session() as session:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {
                pool.submit(
                    _fetch_tile, session, template, view.zoom, x % max_index, y, token
                ): (x, y)
                for x, y in jobs
                if 0 <= y < max_index
            }
            for future, (x, y) in futures.items():
                image = future.result()
                if image is None:
                    continue
                if image.size != (tile, tile):
                    image = image.resize((tile, tile), Image.LANCZOS)
                canvas.paste(
                    image,
                    (int(round(x * tile - left)), int(round(y * tile - top))),
                )
                fetched += 1
    return canvas, fetched


# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------

_FONT_CACHE: dict = {}
FONT_IS_SCALABLE = True

_FONT_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/arial.ttf",
)

_FONT_NAMES = ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf", "LiberationSans-Bold.ttf")


def _font(size: int):
    """A font that actually honours the requested size.

    ``ImageFont.load_default()`` returns a FIXED-SIZE bitmap font - it ignores
    the size argument completely. Falling back to it means every label renders
    at about 11 px however large it was asked to be, and once Excel shrinks the
    image into its slot that is illegible. Streamlit Cloud has no DejaVu
    installed, so this fallback was being hit in production even though it
    never was in development, which is why scaling the sizes changed nothing.

    Pillow 10.1+ can scale its own bundled font through
    ``load_default(size=...)``, needing no system fonts and no extra files.
    """
    global FONT_IS_SCALABLE

    size = max(6, int(round(size)))
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]

    font = None
    for path in _FONT_PATHS:
        try:
            font = ImageFont.truetype(path, size)
            break
        except Exception:  # noqa: BLE001
            continue

    if font is None:
        for name in _FONT_NAMES:
            try:
                font = ImageFont.truetype(name, size)
                break
            except Exception:  # noqa: BLE001
                continue

    if font is None:
        try:
            font = ImageFont.load_default(size=size)
        except TypeError:
            FONT_IS_SCALABLE = False
            font = ImageFont.load_default()

    _FONT_CACHE[size] = font
    return font


def _outlined_text(draw, xy, text, font, fill=LABEL_WHITE, outline=SHADOW, weight=2):
    x, y = xy
    for dx in range(-weight, weight + 1):
        for dy in range(-weight, weight + 1):
            if dx or dy:
                draw.text((x + dx, y + dy), text, font=font, fill=outline)
    draw.text((x, y), text, font=font, fill=fill)


def _centred_text(draw, centre, text, font, **kwargs):
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    _outlined_text(
        draw,
        (centre[0] - (right - left) / 2.0, centre[1] - (bottom - top) / 2.0),
        text,
        font,
        **kwargs,
    )


def _draw_pin(draw, x, y, scale=1.0, colour=(255, 214, 0)):
    """A small Google Earth style pushpin."""
    height = 30 * scale
    radius = 9 * scale
    draw.line([(x, y), (x, y - height + radius)], fill=(60, 60, 60),
              width=max(2, int(3 * scale)))
    draw.ellipse(
        [x - radius, y - height, x + radius, y - height + 2 * radius],
        fill=colour,
        outline=(60, 60, 60),
        width=max(1, int(2 * scale)),
    )


def _draw_flag(draw, x, y, scale=1.0, colour=PIPE_RED):
    """A small flag marker, used for AGMs."""
    height = 32 * scale
    draw.line([(x, y), (x, y - height)], fill=(245, 245, 245),
              width=max(2, int(4 * scale)))
    draw.polygon(
        [(x + scale, y - height),
         (x + 21 * scale, y - height + 8 * scale),
         (x + scale, y - height + 16 * scale)],
        fill=colour,
    )


def _scale_bar(draw, mpp, width, height, scale=1.0):
    """``mpp`` is the ground metres covered by one pixel of the *output* image."""
    feet_per_pixel = mpp * 3.28084
    for target in (200, 400, 500, 800, 1000, 1500, 2000, 3000, 5000):
        pixels = target / feet_per_pixel
        if 90 <= pixels <= width * 0.22:
            break
    else:
        target, pixels = 400, 400 / feet_per_pixel

    right = width - int(70 * scale)
    left = right - pixels
    baseline = height - int(20 * scale)
    thickness = max(2, int(4 * scale))
    tick = 8 * scale
    draw.line([(left, baseline), (right, baseline)], fill=LABEL_WHITE, width=thickness)
    draw.line([(left, baseline - tick), (left, baseline + tick * 0.7)],
              fill=LABEL_WHITE, width=thickness)
    draw.line([(right, baseline - tick), (right, baseline + tick * 0.7)],
              fill=LABEL_WHITE, width=thickness)
    font = _font(int(22 * scale))
    label = f"{target:,} ft"
    label_left, _, label_right, _ = draw.textbbox((0, 0), label, font=font)
    _outlined_text(draw, (right - (label_right - label_left), baseline - 34 * scale), label, font,
                   weight=max(1, int(1.5 * scale)))


def _north_arrow(draw, width, height, scale=1.0):
    font = _font(int(26 * scale))
    x = width - 32 * scale
    y = height - 76 * scale
    draw.polygon(
        [(x, y), (x - 10 * scale, y + 28 * scale),
         (x, y + 20 * scale), (x + 10 * scale, y + 28 * scale)],
        fill=LABEL_WHITE,
        outline=SHADOW,
    )
    _centred_text(draw, (x, y + 46 * scale), "N", font,
                  weight=max(1, int(1.5 * scale)))


def _title_card(draw, text, scale=1.0, width=None):
    if not text:
        return
    pad_x = 11 * scale
    pad_y = 8 * scale
    x0 = 12 * scale
    y0 = 12 * scale
    size = 20 * scale
    font = _font(int(size))
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    # A long dig name is shrunk until the card fits inside the picture.
    while width and (right - left) + pad_x * 2 + x0 * 2 > width and size > 8:
        size -= 1
        font = _font(int(size))
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    box_width = (right - left) + pad_x * 2
    box_height = (bottom - top) + pad_y * 2
    draw.rectangle([x0, y0, x0 + box_width, y0 + box_height],
                   fill=(255, 255, 255, 235), outline=(120, 120, 120),
                   width=max(1, int(scale)))
    draw.text((x0 + pad_x - left, y0 + pad_y - top), text, font=font,
              fill=(20, 20, 20))


# ---------------------------------------------------------------------------
# Legend
# ---------------------------------------------------------------------------

PIN_YELLOW = (255, 214, 0)
NEIGHBOUR_WHITE = (245, 245, 245)


def _layer_count(layer, box) -> int:
    """How many pixels of a transparent layer are drawn inside ``box``."""
    if layer is None:
        return 0
    left, top, right, bottom = (int(round(v)) for v in box)
    left, top = max(left, 0), max(top, 0)
    right, bottom = min(right, layer.width), min(bottom, layer.height)
    if right <= left or bottom <= top:
        return 0
    alpha = layer.crop((left, top, right, bottom)).getchannel("A")
    return alpha.point(lambda value: 255 if value else 0).histogram()[255]


def _legend(layer, entries, width, height, scale=1.0, avoid=None, soft=None):
    """Draw the legend on its own layer, in the corner that hides the least.

    ``entries`` is a list of (label, swatch) or, for a line, (label, 'line', colour);
    swatch is 'line', 'flag' or 'pin'. ``avoid`` is the layer holding the dig pin,
    flags and labels (covering those is what must not happen) and ``soft`` the one
    holding the pipeline lines (covering a stretch of line is a lesser evil).
    """
    if not entries:
        return
    draw = ImageDraw.Draw(layer, "RGBA")

    # Start at a readable size and shrink until the box is a modest part of the
    # picture - a long list of lines must not take over the aerial.
    size = 11.5
    while True:
        unit = size * scale / 17.0
        font = _font(max(8, int(17 * unit)))
        line_height = 20 * unit
        box_width = 120 * unit
        for entry in entries:
            left, top, right, bottom = draw.textbbox((0, 0), entry[0], font=font)
            box_width = max(box_width, (right - left) + 52 * unit)
        box_height = 30 * unit + line_height * len(entries)
        if (box_width <= width * 0.36 and box_height <= height * 0.42) or size <= 7:
            break
        size -= 0.5

    margin = 12 * scale
    candidates = [
        (width - box_width - margin, margin),                          # top right
        (margin, height - box_height - margin),                        # bottom left
        (margin, 62 * scale + margin),                                 # top left, under the title card
        (width - box_width - margin, height - box_height - 100 * scale),  # bottom right, above the scale bar
    ]
    best, best_score = candidates[0], None
    for order, (x, y) in enumerate(candidates):
        x = min(max(x, 0), max(width - box_width, 0))
        y = min(max(y, 0), max(height - box_height, 0))
        box = (x, y, x + box_width, y + box_height)
        score = _layer_count(avoid, box) * 10 + _layer_count(soft, box) + order
        if best_score is None or score < best_score:
            best, best_score = (x, y), score
    x0, y0 = best

    draw.rectangle(
        [x0, y0, x0 + box_width, y0 + box_height],
        fill=(255, 255, 255, 230),
        outline=(120, 120, 120),
        width=max(1, int(scale)),
    )
    draw.text((x0 + 9 * unit, y0 + 5 * unit), "Legend", font=font, fill=(20, 20, 20))
    for index, entry in enumerate(entries):
        label, swatch = entry[0], entry[1]
        y = y0 + 28 * unit + index * line_height
        centre_y = y + line_height * 0.42
        draw.text((x0 + 38 * unit, y), label, font=font, fill=(20, 20, 20))
        if swatch == "line":
            draw.line([(x0 + 9 * unit, centre_y), (x0 + 29 * unit, centre_y)],
                      fill=entry[2] if len(entry) > 2 else PIPE_RED,
                      width=max(2, int(4 * unit)))
        else:
            colour = (entry[2] if swatch == "dot" else PIN_YELLOW if swatch == "pin" else PIPE_RED)
            radius = 5.5 * unit
            draw.ellipse([x0 + 19 * unit - radius, centre_y - radius,
                          x0 + 19 * unit + radius, centre_y + radius],
                         fill=colour, outline=(60, 60, 60))


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def aerial_geometry(latitude: float, span_feet: float, width: int,
                    tile_size: int, max_zoom: int) -> tuple:
    """Pick the tile zoom and resampling factor that make the image exactly
    ``span_feet`` wide on the ground.

    Returns ``(tile_zoom, factor, metres_per_output_pixel)``. Tiles come in
    whole zoom levels, so the canvas is composited at the first level at least
    as detailed as asked for and then resampled by ``1/factor``. (Picking the
    nearest *coarser* level instead - the obvious loop - lands anywhere between
    one and two times wider than requested, so the slider would not mean what
    it says.)
    """
    target_mpp = (span_feet / 3.28084) / width
    mpp_zoom0 = 156543.03392 * math.cos(math.radians(latitude))
    exact = math.log2(mpp_zoom0 / target_mpp)        # zoom needed in a 256 px world
    bonus = math.log2(tile_size / 256.0)             # a 512 px tile is one level denser
    zoom = max(0, min(int(math.ceil(exact - bonus - 1e-9)), max_zoom))
    factor = 2.0 ** (zoom + bonus - exact)           # canvas pixels per output pixel
    return zoom, factor, target_mpp


def _label_line(draw, runs, text, dig_x, dig_y, width, height, scale):
    """Put the pipeline's name beside its line, clear of the dig's pin and label."""
    font = _font(int(22 * scale))
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    text_w, text_h = right - left, bottom - top
    best, best_score = None, None
    for points in runs:
        for (x1, y1), (x2, y2) in zip(points, points[1:]):
            for t in (0.25, 0.5, 0.75):
                x, y = x1 + (x2 - x1) * t, y1 + (y2 - y1) * t
                gap = math.hypot(x - dig_x, y - dig_y)
                if gap < 150 * scale:
                    continue
                box_x = x - text_w / 2
                box_y = y - 30 * scale - text_h
                if (box_x < 6 * scale or box_x + text_w > width - 6 * scale
                        or box_y < 70 * scale or box_y + text_h > height - 70 * scale):
                    continue
                score = abs(gap - 260 * scale)
                if best_score is None or score < best_score:
                    best, best_score = (x, y, box_x, box_y), score
    if best is None:
        return
    x, y, box_x, box_y = best
    draw.line([(x, y - 4 * scale), (x, box_y + text_h + 4 * scale)],
              fill=(255, 255, 255, 255), width=max(2, int(2 * scale)))
    _outlined_text(draw, (box_x, box_y), text, font, weight=max(1, int(2 * scale)))


def _line_runs(line, centre_lat, centre_lon, span_feet):
    """The stretches of a long centerline that matter to one image.

    A pipeline KMZ line has tens of thousands of vertices spanning hundreds of
    miles. Projecting them all at street-level zoom gives coordinates in the
    millions of pixels, which is slow to draw and pointless. Keep only vertices
    inside a generous window, plus the vertex on each side so a segment
    crossing the edge of the picture is still drawn to it.
    """
    half = span_feet * 1.5 / 364000.0                      # degrees of latitude
    half_lon = half / max(math.cos(math.radians(centre_lat)), 0.01)
    inside = [
        abs(lat - centre_lat) <= half and abs(lon - centre_lon) <= half_lon
        for lon, lat in line
    ]
    keep = [
        inside[i]
        or (i > 0 and inside[i - 1])
        or (i + 1 < len(line) and inside[i + 1])
        for i in range(len(line))
    ]
    runs, current = [], []
    for point, wanted in zip(line, keep):
        if wanted:
            current.append(point)
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    return [run for run in runs if len(run) >= 2]


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

WELD_RED = (255, 0, 0)          # the KMZ's own weld / tie-in icon colours
TIE_IN_GREEN = (0, 255, 0)
AUTO_SPAN_MIN_FEET = 1500.0
AUTO_SPAN_MAX_FEET = 6000.0


class AerialError(RuntimeError):
    pass


def auto_span_feet(dig, kmz: Optional[KmzData], aspect: float) -> tuple:
    """(span_feet, centre_lat, centre_lon) that keeps the dig and its nearest AGM in view.

    Both points are placed inside the picture with room for what is drawn around them:
    the title card and the pin / flag stand above their point, and the labels sit
    beside or below it. When the two cannot fit within the maximum span the picture
    is centred on the dig and the AGM is simply off the edge.
    """
    lat, lon = float(dig.latitude), float(dig.longitude)
    nearest = None
    if dig.us_ref_distance is not None and dig.ds_ref_distance is not None:
        nearest = dig.upstream_reference if dig.us_ref_distance <= dig.ds_ref_distance \
            else dig.downstream_reference
    elif dig.upstream_reference:
        nearest = dig.upstream_reference
    point = kmz.marker(nearest) if (kmz is not None and nearest) else None
    if point is None:
        return AUTO_SPAN_MIN_FEET, lat, lon

    cos = math.cos(math.radians(lat))
    dx = abs(point[1] - lon) * 364000.0 * cos
    dy = abs(point[0] - lat) * 364000.0
    # Usable share of the picture: 70% of the width, and the band between the
    # title card / pin above (top 28%) and the labels below (bottom 20%).
    span = max(AUTO_SPAN_MIN_FEET, dx / 0.70, dy * aspect / 0.52)
    if span > AUTO_SPAN_MAX_FEET:
        return AUTO_SPAN_MAX_FEET, lat, lon
    height_feet = span / aspect
    north, south = (lat, point[0]) if lat >= point[0] else (point[0], lat)
    mid_lat = (north + south) / 2.0
    # The pair's midpoint belongs 4% of the height below the picture's centre.
    centre_lat = mid_lat + 0.04 * height_feet / 364000.0
    return span, centre_lat, (lon + point[1]) / 2.0


def render_aerial(
    dig,
    kmz: Optional[KmzData],
    width: int = 1600,
    basemap: str = "Esri World Imagery",
    span_feet: Optional[float] = None,
    show_legend: bool = True,
    token: str = "",
    slot_aspect: Optional[float] = None,
    others: Iterable = (),
) -> Optional[bytes]:
    """JPEG bytes for one dig, or None when the dig has no coordinate.

    ``span_feet`` None means automatic: wide enough to hold the dig and its nearest
    AGM. ``slot_aspect`` is the aspect of the template's image slot; rendering to it
    exactly means the picture is never cropped to fit.
    """
    if dig.latitude is None or dig.longitude is None:
        return None

    aspect = slot_aspect or SLOT_ASPECT
    height = int(round(width / aspect))
    scale = width / 640.0                 # labels are sized for a ~650 px wide slot
    dig_lat, dig_lon = float(dig.latitude), float(dig.longitude)

    if span_feet is None:
        span_feet, latitude, longitude = auto_span_feet(dig, kmz, aspect)
    else:
        latitude, longitude = dig_lat, dig_lon

    spec = BASEMAPS.get(basemap) or BASEMAPS["Esri World Imagery"]
    tile_size = spec["tile_size"]
    tile_token = token if spec["needs_token"] else ""
    if spec["needs_token"] and not tile_token:
        raise AerialError(f"{basemap} needs a Mapbox token.")

    zoom, factor, mpp = aerial_geometry(latitude, span_feet, width, tile_size,
                                        spec.get("max_zoom", 19))
    canvas_width = max(1, int(round(width * factor)))
    canvas_height = max(1, int(round(height * factor)))
    view = MapView(zoom=zoom, centre_lat=latitude, centre_lon=longitude,
                   width=canvas_width, height=canvas_height, tile_size=tile_size)

    image, fetched = _basemap_image(view, spec["url"], tile_token)
    if fetched == 0:
        raise AerialError(
            f"No imagery tiles could be fetched from {basemap}. "
            "Check network access, or the Mapbox token if using Mapbox."
        )
    if image.size != (width, height):
        image = image.resize((width, height), Image.LANCZOS)

    def project(lon, lat):
        x, y = view.project(lon, lat)
        return x / factor, y / factor

    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay, "RGBA")
    legend_entries = []
    this_rep = kmz.replacements.get(rep_number(dig)) if kmz else None

    # --- KMZ lines, each in the colour the KMZ gives it ---------------------
    # The window is generous: a segment that crosses the picture's edge must still
    # be drawn to it.
    if kmz is not None:
        half_lat = span_feet * 1.5 / 364000.0
        half_lon = half_lat / max(math.cos(math.radians(latitude)), 0.01)
        win = (longitude - half_lon, latitude - half_lat, longitude + half_lon, latitude + half_lat)
        visible = [
            line for line in kmz.lines
            if line.bbox[2] >= win[0] and line.bbox[0] <= win[2]
            and line.bbox[3] >= win[1] and line.bbox[1] <= win[3]
        ]
        is_span = lambda line: bool(re.match(r"^\s*REP\s*\d+\s*Span\s*$", line.name, re.I))  # noqa: E731
        base_width = max(3, int(3 * scale))
        seen = set()
        for line in visible:
            if is_span(line):
                continue
            for run in _line_runs(line.coords, latitude, longitude, span_feet):
                draw.line([project(lon, lat) for lon, lat in run],
                          fill=line.colour + (230,), width=base_width, joint="curve")
            key = (line.name, line.colour)
            if line.name and key not in seen:
                seen.add(key)
                legend_entries.append((line.name, "line", line.colour))
        for line in visible:
            if not is_span(line):
                continue
            mine = this_rep is not None and line is this_rep.span
            thickness = max(5, int((9 if mine else 5) * scale))
            for run in _line_runs(line.coords, latitude, longitude, span_feet):
                draw.line([project(lon, lat) for lon, lat in run],
                          fill=line.colour + (255 if mine else 190,), width=thickness, joint="curve")
        if this_rep is not None and this_rep.span in visible:
            legend_entries.append((f"REP {this_rep.number} span", "line", this_rep.span.colour))
        elif any(is_span(line) for line in visible):
            legend_entries.append(("Replacement span", "line", KML_DEFAULT_LINE))

    lines_layer = overlay
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay, "RGBA")

    dig_x, dig_y = project(dig_lon, dig_lat)

    # --- this replacement's weld and tie-in points ---------------------------
    if this_rep is not None:
        radius = 4.5 * scale
        added = set()
        for kind, colour, label in (("uTie_in", TIE_IN_GREEN, "Tie-in"), ("dTie_in", TIE_IN_GREEN, "Tie-in"),
                                    ("uGW", WELD_RED, "Girth weld"), ("dGW", WELD_RED, "Girth weld")):
            point = this_rep.points.get(kind)
            if point is None:
                continue
            px, py = project(point[1], point[0])
            if not (0 <= px <= width and 0 <= py <= height):
                continue
            draw.ellipse([px - radius, py - radius, px + radius, py + radius],
                         fill=colour, outline=(40, 40, 40), width=max(1, int(1.5 * scale)))
            if label not in added:
                added.add(label)
                legend_entries.append((f"{label}s", "dot", colour))

    # --- other digs, when they fall in the picture ---------------------------
    for other in others:
        if other is dig or other.latitude is None or other.longitude is None:
            continue
        ox, oy = project(other.longitude, other.latitude)
        if not (0 <= ox <= width and 0 <= oy <= height):
            continue
        if math.hypot(ox - dig_x, oy - dig_y) < 70 * scale:
            continue
        radius = 6 * scale
        draw.ellipse([ox - radius, oy - radius, ox + radius, oy + radius],
                     fill=NEIGHBOUR_WHITE, outline=(60, 60, 60), width=max(1, int(2 * scale)))
        _outlined_text(draw, (ox + 10 * scale, oy - 14 * scale), other.label or other.name,
                       _font(int(20 * scale)), weight=max(1, int(1.5 * scale)))

    # --- the dig's U/S and D/S references (AGMs from the KMZ) ----------------
    if kmz is not None:
        for label in (dig.upstream_reference, dig.downstream_reference):
            point = kmz.marker(label) if label else None
            if point is None:
                continue
            ax, ay = project(point[1], point[0])
            if not (-50 <= ax <= width + 50 and -50 <= ay <= height + 50):
                continue
            _draw_flag(draw, ax, ay, scale)
            label_font = _font(int(24 * scale))
            left, top, right, bottom = draw.textbbox((0, 0), label, font=label_font)
            label_width = right - left
            if ax + 26 * scale + label_width > width * 0.62:
                label_x = ax - 12 * scale - label_width
            else:
                label_x = ax + 26 * scale
            _outlined_text(draw, (label_x, ay - 40 * scale), label, label_font,
                           weight=max(1, int(2 * scale)))
            legend_entries.append((label, "flag"))

    # --- the dig ---------------------------------------------------------------
    pin_text = dig.label or dig.name
    _draw_pin(draw, dig_x, dig_y, scale)
    _centred_text(draw, (dig_x, dig_y + 34 * scale), pin_text,
                  _font(int(32 * scale)), weight=max(2, int(2.5 * scale)))
    if pin_text:
        legend_entries.append((pin_text, "pin"))

    _title_card(draw, dig.name, scale, width)
    _scale_bar(draw, mpp, width, height, scale)
    _north_arrow(draw, width, height, scale)

    legend_layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    if show_legend:
        _legend(legend_layer, legend_entries, width, height, scale,
                avoid=overlay, soft=lines_layer)

    image = image.convert("RGBA")
    for layer in (lines_layer, overlay, legend_layer):
        image = Image.alpha_composite(image, layer)
    image = image.convert("RGB")

    # JPEG keeps a batch of reports small; chroma subsampling off keeps text crisp.
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90, subsampling=0, optimize=True)
    return buffer.getvalue()


# ==========================================================================
# DIRECTIONS
# ==========================================================================
# Driving directions, ported from Hayden's Dig Site Directions Generator.
#
# Source: github.com/maydenhiller/Dig-Site-Directions-Generator
#
# The logic is kept identical so the wording matches the reports already issued:
# reverse geocode the dig to its town, seed a route from the town centre to get a
# real road start point, name that start point by the two distinct roads meeting
# there, then phrase each step as "<instruction> and continue traveling
# <cardinal> for <n.nn> miles", closing with which side the dig sits on relative
# to the direction of travel.
#
# Needs a Mapbox token - the same one the existing app uses.

TIMEOUT = 25
CARDINALS = ["North", "Northeast", "East", "Southeast",
             "South", "Southwest", "West", "Northwest"]


class DirectionsError(RuntimeError):
    pass


@dataclass
class DirectionsResult:
    paragraph: str
    town: str = ""
    state: str = ""
    intersection: str = ""


# ---------------------------------------------------------------------------
# Web Mercator geometry - used to decide which side of the road the dig is on
# ---------------------------------------------------------------------------

def _mercator_xy(longitude: float, latitude: float) -> tuple:
    radius = 6378137.0
    x = math.radians(longitude) * radius
    y = math.log(math.tan(math.pi / 4 + math.radians(latitude) / 2)) * radius
    return x, y


def _segment_projection(a_lon, a_lat, b_lon, b_lat, p_lon, p_lat):
    ax, ay = _mercator_xy(a_lon, a_lat)
    bx, by = _mercator_xy(b_lon, b_lat)
    px, py = _mercator_xy(p_lon, p_lat)
    vx, vy = bx - ax, by - ay
    length_squared = vx * vx + vy * vy
    if length_squared == 0:
        return 0.0, ax, ay, ax, ay, bx, by
    t = ((px - ax) * vx + (py - ay) * vy) / length_squared
    t = max(0.0, min(1.0, t))
    return t, ax + t * vx, ay + t * vy, ax, ay, bx, by


def _nearest_segment(route, dig_lat, dig_lon):
    best, best_distance = None, float("inf")
    px, py = _mercator_xy(dig_lon, dig_lat)
    for index in range(len(route) - 1):
        a_lat, a_lon = route[index]
        b_lat, b_lon = route[index + 1]
        _, projx, projy, *_ = _segment_projection(
            a_lon, a_lat, b_lon, b_lat, dig_lon, dig_lat
        )
        dx, dy = px - projx, py - projy
        distance = dx * dx + dy * dy
        if distance < best_distance:
            best_distance = distance
            best = ((a_lon, a_lat), (b_lon, b_lat), (projx, projy))
    return best


def side_relative_to_route(route, dig_lat, dig_lon) -> str:
    nearest = _nearest_segment(route, dig_lat, dig_lon)
    if not nearest:
        return "right"
    (a_lon, a_lat), (b_lon, b_lat), (projx, projy) = nearest
    ax, ay = _mercator_xy(a_lon, a_lat)
    bx, by = _mercator_xy(b_lon, b_lat)
    px, py = _mercator_xy(dig_lon, dig_lat)
    cross = (bx - ax) * (py - projy) - (by - ay) * (px - projx)
    return "left" if cross > 0 else "right"


# ---------------------------------------------------------------------------
# Phrasing
# ---------------------------------------------------------------------------

def _bearing_to_cardinal(bearing: float) -> str:
    return CARDINALS[round((bearing % 360) / 45) % 8]


def _extract_town_state(feature) -> tuple:
    town, state = "", ""
    for context in feature.get("context", []):
        identifier = context.get("id", "")
        if identifier.startswith("place."):
            town = context.get("text", "")
        if identifier.startswith("region."):
            state = context.get("text", "")
    if not town:
        town = feature.get("text", "")
    return town, state


def _normalise_street_base(name: str) -> str:
    if not name:
        return ""
    text = re.sub(r"^(N|S|E|W)\s+", "", name.strip(), flags=re.I)
    text = re.sub(
        r"\b(Street|St\.?|Avenue|Ave\.?|Road|Rd\.?|Boulevard|Blvd\.?|Drive|Dr\.?"
        r"|Lane|Ln\.?|Terrace|Ter\.?|Court|Ct\.?)\b\.?",
        "",
        text,
        flags=re.I,
    ).strip()
    return re.sub(r"\s{2,}", " ", text)


def _intersection_label(longitude, latitude, token) -> str:
    url = (
        "https://api.mapbox.com/v4/mapbox.mapbox-streets-v8/tilequery/"
        f"{longitude},{latitude}.json?layers=road&radius=300&limit=50"
        f"&access_token={token}"
    )
    payload = requests.get(url, timeout=TIMEOUT).json()

    seen, roads = set(), []
    for feature in payload.get("features", []):
        properties = feature.get("properties", {})
        name = properties.get("name")
        if not name or name in seen:
            continue
        seen.add(name)
        roads.append((name, properties.get("class", ""), _normalise_street_base(name)))

    if not roads:
        return "Unknown Intersection"

    bases = [base for _, _, base in roads]
    if any("Washington" in b for b in bases) and any("Jefferson" in b for b in bases):
        washington = next(n for n, _, b in roads if "Washington" in b)
        jefferson = next(n for n, _, b in roads if "Jefferson" in b)
        return f"{washington} & {jefferson}"

    used, chosen = set(), []
    for name, _, base in roads:
        if base not in used:
            chosen.append(name)
            used.add(base)
        if len(chosen) == 2:
            break
    return " & ".join(chosen) if chosen else "Unknown Intersection"


def _format_step(step, miles: float) -> str:
    manoeuvre = step.get("maneuver", {})
    instruction = manoeuvre.get("instruction", "").rstrip(".")
    cardinal = _bearing_to_cardinal(manoeuvre.get("bearing_after", 0))
    return f"{instruction} and continue traveling {cardinal} for {miles:.2f} miles"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def generate_directions(latitude: float, longitude: float, token: str) -> DirectionsResult:
    if not token:
        raise DirectionsError("No Mapbox token configured.")

    import polyline

    town_url = (
        "https://api.mapbox.com/geocoding/v5/mapbox.places/"
        f"{longitude},{latitude}.json?types=place&language=en&access_token={token}"
    )
    town_payload = requests.get(town_url, timeout=TIMEOUT).json()
    features = town_payload.get("features") or []
    if not features:
        raise DirectionsError("Mapbox could not find a town near this coordinate.")
    town_feature = features[0]
    town, state = _extract_town_state(town_feature)
    town_centre = town_feature["center"]

    seed_url = (
        "https://api.mapbox.com/directions/v5/mapbox/driving/"
        f"{town_centre[0]},{town_centre[1]};{longitude},{latitude}"
        "?steps=true&geometries=polyline&overview=full&language=en"
        f"&access_token={token}"
    )
    seed = requests.get(seed_url, timeout=TIMEOUT).json()
    seed_routes = seed.get("routes") or []
    if not seed_routes:
        raise DirectionsError("Mapbox could not route to this coordinate.")
    start = seed_routes[0]["legs"][0]["steps"][0]["maneuver"]["location"]

    intersection = _intersection_label(start[0], start[1], token)

    route_url = (
        "https://api.mapbox.com/directions/v5/mapbox/driving/"
        f"{start[0]},{start[1]};{longitude},{latitude}"
        "?steps=true&geometries=polyline&overview=full&language=en"
        f"&access_token={token}"
    )
    payload = requests.get(route_url, timeout=TIMEOUT).json()
    routes = payload.get("routes") or []
    if not routes:
        raise DirectionsError("Mapbox returned no route from the start intersection.")
    route = routes[0]
    steps = route["legs"][0]["steps"]
    coordinates = polyline.decode(route["geometry"])

    narrative = [
        f"From the intersection of {intersection} in {town}, {state}, travel as follows"
    ]
    for index, step in enumerate(steps):
        if index == len(steps) - 1:
            side = side_relative_to_route(coordinates, latitude, longitude)
            narrative.append(f"The dig site will be located on your {side}.")
        else:
            narrative.append(_format_step(step, step["distance"] / 1609.34))

    paragraph = " ".join(line.strip().rstrip(".") + "." for line in narrative)
    return DirectionsResult(
        paragraph=paragraph, town=town, state=state, intersection=intersection
    )


# ==========================================================================
# COUNTY / STATE
# ==========================================================================
# County and state come from the dig's GPS coordinates. Three free services are
# tried in turn so one outage does not leave a report blank; the first that
# answers wins. Nothing is guessed: if all three fail the fields stay "Unknown".

class GeocodeError(RuntimeError):
    pass


def _strip_county(name: str) -> str:
    return re.sub(r"\s+County$", "", (name or "").strip(), flags=re.I)


def _county_from_fcc(latitude: float, longitude: float) -> tuple:
    payload = requests.get(
        "https://geo.fcc.gov/api/census/area",
        params={"lat": latitude, "lon": longitude, "format": "json"},
        timeout=TIMEOUT, headers={"User-Agent": USER_AGENT},
    ).json()
    results = payload.get("results") or []
    if not results or not results[0].get("county_name"):
        raise GeocodeError("FCC returned no county.")
    return _strip_county(results[0]["county_name"]), results[0].get("state_name", "")


def _county_from_census(latitude: float, longitude: float) -> tuple:
    payload = requests.get(
        "https://geocoding.geo.census.gov/geocoder/geographies/coordinates",
        params={"x": longitude, "y": latitude, "benchmark": "Public_AR_Current",
                "vintage": "Current_Current", "layers": "States,Counties", "format": "json"},
        timeout=TIMEOUT, headers={"User-Agent": USER_AGENT},
    ).json()
    geographies = (payload.get("result") or {}).get("geographies") or {}
    counties, states = geographies.get("Counties") or [], geographies.get("States") or []
    if not counties or not states:
        raise GeocodeError("The Census geocoder returned no county.")
    return _strip_county(counties[0].get("BASENAME") or counties[0].get("NAME", "")), states[0].get("NAME", "")


def _county_from_mapbox(latitude: float, longitude: float, token: str) -> tuple:
    if not token:
        raise GeocodeError("No Mapbox token.")
    payload = requests.get(
        f"https://api.mapbox.com/geocoding/v5/mapbox.places/{longitude},{latitude}.json",
        params={"types": "district,region", "language": "en", "access_token": token},
        timeout=TIMEOUT,
    ).json()
    county = state = ""
    for feature in payload.get("features") or []:
        kind = (feature.get("id") or "").split(".")[0]
        if kind == "district" and not county:
            county = _strip_county(feature.get("text", ""))
        elif kind == "region" and not state:
            state = feature.get("text", "")
        for context in feature.get("context") or []:
            kind = (context.get("id") or "").split(".")[0]
            if kind == "district" and not county:
                county = _strip_county(context.get("text", ""))
            elif kind == "region" and not state:
                state = context.get("text", "")
    if not county or not state:
        raise GeocodeError("Mapbox returned no county.")
    return county, state


def county_and_state(latitude: float, longitude: float, token: str = "") -> tuple:
    """(county, state), e.g. ('Wichita', 'Texas'), from GPS coordinates."""
    errors = []
    for lookup in (lambda: _county_from_fcc(latitude, longitude),
                   lambda: _county_from_census(latitude, longitude),
                   lambda: _county_from_mapbox(latitude, longitude, token)):
        try:
            county, state = lookup()
            if county and state:
                return county, state
        except Exception as error:  # noqa: BLE001 - any network/parse failure falls through
            errors.append(str(error))
    raise GeocodeError("County/state lookup failed: " + "; ".join(errors))


# ==========================================================================
# CHEATSHEET
# ==========================================================================
# Fill the dig stake cheat sheet - three rows per dig: U/S weld, anomaly, D/S weld.
#
# The template ships in two flavours, ascending and descending station numbers. They
# differ only in the sign of the station formulas, so the right formulas are written
# per dig from the stationing direction read off that dig's own references. When both
# templates are uploaded the one matching the dig sheets' direction is used.
#
# Stationing and odometer are different scales and are not expected to agree, so the
# station is built from the U/S reference's printed station plus the odometer offset
# from that reference (Dig.station_feet) and is never checked against the odometer.

FIRST_ROW = 6
ROWS_PER_DIG = 3

COL_NAME = 1          # A
COL_ODO = 2           # B
COL_STATION = 3       # C
COL_FROM_ANOMALY = 4  # D
COL_FROM_REF = 5      # E
COL_NOTES = 6         # F
COL_HCA = 7           # G
COL_DEPTH = 8         # H
COL_LAT = 9           # I
COL_LON = 10          # J


def station_ascends(dig: Dig) -> bool:
    """True when stationing rises with odometer, read from the dig's own two references."""
    us = Dig._station_feet(dig.us_ref_station)
    ds = Dig._station_feet(dig.ds_ref_station)
    if us is None or ds is None or us == ds:
        return True
    if dig.us_ref_odometer is not None and dig.ds_ref_odometer is not None:
        return (ds - us) * (dig.ds_ref_odometer - dig.us_ref_odometer) > 0
    return ds > us


def cheat_sheet_flavour(template_bytes: bytes) -> Optional[str]:
    """'ascending' or 'descending', read from the template's own U/S weld formula."""
    import openpyxl

    try:
        sheet = openpyxl.load_workbook(io.BytesIO(template_bytes)).worksheets[0]
    except Exception:  # noqa: BLE001
        return None
    formula = str(sheet.cell(FIRST_ROW, COL_STATION).value or "").replace(" ", "")
    if re.fullmatch(r"=C\d+-D\d+", formula):
        return "ascending"
    if re.fullmatch(r"=C\d+\+D\d+", formula):
        return "descending"
    return None


def pick_cheat_template(templates: dict, digs: Iterable[Dig]) -> Optional[bytes]:
    """``templates`` maps flavour (or None) -> bytes. Prefer the dig sheets' own direction."""
    if not templates:
        return None
    digs = list(digs)
    ascending = sum(1 for d in digs if station_ascends(d))
    wanted = "ascending" if ascending * 2 >= len(digs) else "descending"
    if wanted in templates:
        return templates[wanted]
    return next(iter(templates.values()))


def cheat_name(dig: Dig, full_names: bool) -> str:
    """Column A name. 'Dig 4' by default - the full heading is far wider than the column."""
    return (dig.name if full_names else (dig.label or dig.name)) or "Dig"


def build_cheat_sheet(
    template_bytes: bytes,
    digs: Iterable[Dig],
    auto_notes: bool = True,
    proximity_feet: float = 2000.0,
    full_names: bool = False,
) -> bytes:
    import openpyxl

    workbook = openpyxl.load_workbook(io.BytesIO(template_bytes))
    sheet = workbook.worksheets[0]

    digs = sorted(digs, key=lambda d: (d.group, d.odometer is None, d.odometer or 0.0))
    _ensure_rows(sheet, len(digs))

    previous: Optional[Dig] = None
    for index, dig in enumerate(digs):
        top = FIRST_ROW + index * ROWS_PER_DIG
        anomaly = top + 1
        bottom = top + 2
        name = cheat_name(dig, full_names)

        sheet.cell(top, COL_NAME).value = f"{name} U/S Weld"
        sheet.cell(anomaly, COL_NAME).value = f"{name} Anomaly"
        sheet.cell(bottom, COL_NAME).value = f"{name} D/S Weld"

        station = dig.station_feet
        sheet.cell(anomaly, COL_ODO).value = _round2(dig.odometer)
        sheet.cell(anomaly, COL_STATION).value = None if station is None else int(round(station))

        # Weld distances sit on the weld rows; the formulas above and below the
        # anomaly derive the weld odometer and station from them.
        sheet.cell(top, COL_FROM_ANOMALY).value = _round2(dig.us_weld_distance)
        sheet.cell(bottom, COL_FROM_ANOMALY).value = _round2(dig.ds_weld_distance)

        # The U/S weld is always at the lower odometer, so the ODO formulas never
        # change. Only the station column flips with the stationing direction.
        sheet.cell(top, COL_ODO).value = f"=B{anomaly}-D{top}"
        sheet.cell(bottom, COL_ODO).value = f"=B{anomaly}+D{bottom}"
        up, down = ("-", "+") if station_ascends(dig) else ("+", "-")
        sheet.cell(top, COL_STATION).value = f"=C{anomaly}{up}D{top}"
        sheet.cell(bottom, COL_STATION).value = f"=C{anomaly}{down}D{bottom}"

        sheet.cell(anomaly, COL_FROM_REF).value = dig.nearest_reference_label or None

        sheet.cell(anomaly, COL_NOTES).value = None
        if (auto_notes and previous is not None and previous.group == dig.group
                and dig.odometer and previous.odometer):
            gap = abs(dig.odometer - previous.odometer)
            if gap <= proximity_feet:
                sheet.cell(anomaly, COL_NOTES).value = (
                    f"{gap:.2f}' past {cheat_name(previous, full_names)}")
        if dig.notes:
            sheet.cell(anomaly, COL_NOTES).value = dig.notes

        sheet.cell(anomaly, COL_HCA).value = dig.hca_label or None

        # Latitude and longitude come straight from the dig sheet, on the anomaly
        # row. The weld rows are cleared so template leftovers cannot leak through.
        for row in (top, anomaly, bottom):
            sheet.cell(row, COL_LAT).value = None
            sheet.cell(row, COL_LON).value = None
        sheet.cell(anomaly, COL_LAT).value = dig.latitude
        sheet.cell(anomaly, COL_LON).value = dig.longitude

        previous = dig

    _clear_unused(sheet, len(digs))

    # Eight-decimal coordinates need more room than the template's columns give them.
    for letter in ("I", "J"):
        dimension = sheet.column_dimensions[letter]
        if (dimension.width or 0) < 36:
            dimension.width = 36

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _ensure_rows(sheet, dig_count: int) -> None:
    """Extend the template past its pre-built dig blocks if needed."""
    needed = FIRST_ROW + dig_count * ROWS_PER_DIG - 1
    if needed <= sheet.max_row:
        return
    for row in range(sheet.max_row + 1, needed + 1):
        source_row = FIRST_ROW + ((row - FIRST_ROW) % ROWS_PER_DIG)
        for column in range(1, COL_LON + 1):
            source = sheet.cell(source_row, column)
            target = sheet.cell(row, column)
            if source.has_style:
                target._style = copy(source._style)
        height = sheet.row_dimensions[source_row].height
        if height:
            sheet.row_dimensions[row].height = height


def _clear_unused(sheet, dig_count: int) -> None:
    """Blank out the template's leftover 'Dig #n' placeholder blocks."""
    start = FIRST_ROW + dig_count * ROWS_PER_DIG
    for row in range(start, sheet.max_row + 1):
        for column in range(1, COL_LON + 1):
            sheet.cell(row, column).value = None


# ==========================================================================
# STAKING
# ==========================================================================
# Write one filled staking report per dig, from the uploaded .xlsm template.
#
# The report's cells are located by their printed labels, not by fixed addresses:
# "Alignment Sheet:" in column A says where the alignment sheet goes, "Upstream
# Reference" says where the dig name and the U/S reference sit, and so on. If a
# template has been laid out differently - rows inserted, a label reworded - a
# cell whose label cannot be found is left alone and reported, never guessed at.
#
# Never written, whatever the template: the phone number (C12 is a lookup keyed off
# the surveyor's name), the survey date, and the field-measured latitude, longitude,
# elevation and EDOC. Staking notes stay exactly as the template has them.

SHEET = "StakingReport"
CONTACTS_SHEET = "Contacts"


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def _unescape(text: str) -> str:
    return (text.replace("&apos;", "'").replace("&quot;", '"')
            .replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&"))


def _numeric_if_possible(value):
    """Tract '4' -> 4 so it matches the numeric cell the crews' reports have."""
    text = _text(value)
    if re.fullmatch(r"\d+", text):
        return int(text)
    return text


@dataclass
class ReportSettings:
    surveyor_name: str = ""


@dataclass
class TemplateInfo:
    """What the staking report template says about itself."""

    stakers: list = field(default_factory=list)        # Contacts!A, in order
    phones: dict = field(default_factory=dict)         # name -> phone
    staking_notes: str = ""
    slot_aspect: Optional[float] = None
    layout: dict = field(default_factory=dict)         # field -> cell address
    # What the two cells under the references hold: "feet" (distance to the AGM, the
    # ONEOK layout) or "station" (the reference's own station, the HF Sinclair layout).
    detail_kind: str = "feet"
    # True when the dig-name box is laid out "Dig#XXXX ODO XXXXX.XX": name and odometer
    # share the one cell and there are no separate ODO / Stationing cells.
    name_has_odo: bool = False
    problems: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Reading a sheet's text once
# ---------------------------------------------------------------------------

_CELL_RE = re.compile(r"<c\b([^>]*?)(?:/>|>(.*?)</c>)", re.S)


def sheet_texts(patcher: XlsmPatcher, sheet_name: str) -> dict:
    """{'A4': 'Upstream Reference', ...} for every cell with text or a cached value."""
    path = patcher._sheet_paths.get(sheet_name)
    if path is None:
        return {}
    shared = []
    if "xl/sharedStrings.xml" in patcher.parts:
        shared = [
            _unescape("".join(re.findall(r"<t[^>]*>(.*?)</t>", item, re.S)))
            for item in re.findall(r"<si>(.*?)</si>", patcher._text("xl/sharedStrings.xml"), re.S)
        ]
    out = {}
    for attrs, body in _CELL_RE.findall(patcher._text(path)):
        if not body:
            continue
        ref = re.search(r'\br="([A-Z]+\d+)"', attrs)
        if not ref:
            continue
        kind = re.search(r'\bt="(\w+)"', attrs)
        kind = kind.group(1) if kind else ""
        if kind == "inlineStr":
            text = _unescape("".join(re.findall(r"<t[^>]*>(.*?)</t>", body, re.S)))
        else:
            value = re.search(r"<v>(.*?)</v>", body, re.S)
            if value is None:
                continue
            text = (shared[int(value.group(1))] if kind == "s" and int(value.group(1)) < len(shared)
                    else _unescape(value.group(1)))
        if text.strip():
            out[ref.group(1)] = text.strip()
    return out


# ---------------------------------------------------------------------------
# Layout: which cell holds what
# ---------------------------------------------------------------------------

# (field, label to find, column the label must sit in, value column, row offset
# from the label's row)
_LAYOUT = (
    ("dig_name",    "Upstream Reference",   "A", "G", 0),
    ("up_ref",      "Upstream Reference",   "A", "A", 1),
    ("up_feet",     "Upstream Reference",   "A", "A", 2),
    ("line_name",   "Upstream Reference",   "A", "J", -2),
    ("tool_type",   "Upstream Reference",   "A", "J", -1),
    ("down_ref",    "Downstream Reference", "N", "N", 1),
    ("down_feet",   "Downstream Reference", "N", "N", 2),
    ("odometer",    "ODO:",                 "E", "F", 0),
    ("station",     "Stationing:",          "J", "K", 0),
    ("surveyor",    "Surveyed By:",         "A", "C", 0),
    ("alignment",   "Alignment Sheet:",     "A", "C", 0),
    ("tract",       "Tract Number:",        "A", "C", 0),
    ("legal",       "Legal Description:",   "A", "C", 0),
    ("county",      "County:",              "A", "C", 0),
    ("state",       "State:",               "A", "C", 0),
    ("hca",         "HCA",                  "A", "C", 0),
    ("directions",  "Directions",           "A", "A", 1),
)
_WRAPPED_FIELDS = ("directions",)
_SHRINK_FIELDS = ("dig_name", "up_ref", "up_feet", "down_ref", "down_feet", "line_name",
                  "tool_type", "surveyor", "alignment", "tract", "legal", "county", "state", "hca")


def _normal_label(text: str) -> str:
    return re.sub(r"[\s:]+", " ", text).strip().lower()


def find_layout(texts: dict) -> tuple:
    """({field: 'C19'}, [problems]) - cells located by their labels."""
    by_label = {}
    for ref, text in texts.items():
        match = re.match(r"([A-Z]+)(\d+)$", ref)
        if match:
            by_label.setdefault(_normal_label(text), []).append((match.group(1), int(match.group(2))))

    layout, problems = {}, []
    for name, label, label_col, value_col, offset in _LAYOUT:
        hits = [h for h in by_label.get(_normal_label(label), []) if h[0] == label_col]
        if not hits:
            problems.append(f"Label '{label}' not found in column {label_col} - {name.replace('_', ' ')} left unwritten.")
            continue
        row = min(h[1] for h in hits) + offset
        if row < 1:
            problems.append(f"{name.replace('_', ' ')} would fall above the sheet - left unwritten.")
            continue
        layout[name] = f"{value_col}{row}"

    # Line name and tool type have no label of their own; the client banner sits one
    # row above them, so require it before trusting the two cells.
    for name in ("line_name", "tool_type"):
        if name in layout:
            row = int(layout[name][1:])
            banner = f"J{row - (1 if name == 'line_name' else 2)}"
            if banner not in texts:
                del layout[name]
                problems.append(f"No client banner above {name.replace('_', ' ')} - left unwritten.")
    return layout, problems


def read_template_info(template_bytes: bytes) -> TemplateInfo:
    """Names, phone numbers, default notes, cell layout and image slot of a template."""
    info = TemplateInfo()
    try:
        patcher = XlsmPatcher(template_bytes)
    except Exception as error:  # noqa: BLE001
        info.problems.append(f"Not a readable workbook: {error}")
        return info

    names = patcher.sheet_names()
    if SHEET not in names:
        info.problems.append(f"The template has no '{SHEET}' sheet - found {', '.join(names)}.")
        return info

    texts = sheet_texts(patcher, SHEET)
    info.layout, layout_problems = find_layout(texts)
    if "dig_name" in info.layout:
        placeholder = texts.get(info.layout["dig_name"], "")
        info.name_has_odo = re.search(r"\bODO\b", placeholder, re.I) is not None
    if info.name_has_odo:
        # The odometer is written into the dig-name box; there are no ODO / Stationing cells.
        layout_problems = [p for p in layout_problems
                           if "'ODO:'" not in p and "'Stationing:'" not in p]
        info.layout.pop("odometer", None)
        info.layout.pop("station", None)
    if "up_feet" in info.layout:
        detail = texts.get(info.layout["up_feet"], "")
        info.detail_kind = "station" if re.search(r"station", detail, re.I) else "feet"
    # Where the references' stationing sits under their names (HF Sinclair), the feet
    # from each AGM to the dig go on the row between them, in E and K.
    if info.detail_kind == "station" and "up_feet" in info.layout:
        gap_row = int(info.layout["up_feet"][1:]) - 1
        for name, column in (("up_gap", "E"), ("down_gap", "K")):
            if f"{column}{gap_row}" not in texts:
                info.layout[name] = f"{column}{gap_row}"
    info.problems.extend(layout_problems)

    notes = _find_notes_cell(texts)
    info.staking_notes = texts.get(notes, "") if notes else ""

    slot = patcher.find_image_slot(SHEET)
    if slot is not None:
        info.slot_aspect = slot.aspect
    else:
        info.problems.append("No image placeholder found on the StakingReport sheet.")

    if CONTACTS_SHEET in names:
        contacts = sheet_texts(patcher, CONTACTS_SHEET)
        for row in range(2, 51):
            name = contacts.get(f"A{row}", "")
            if name:
                info.stakers.append(name)
                info.phones[name] = contacts.get(f"B{row}", "")
    return info


def _find_notes_cell(texts: dict) -> Optional[str]:
    for ref, text in texts.items():
        if _normal_label(text) == "staking notes":
            match = re.match(r"([A-Z]+)(\d+)$", ref)
            return f"{match.group(1)}{int(match.group(2)) + 1}"
    return None


def template_image_aspect(template_bytes: bytes) -> Optional[float]:
    """The aspect of the image slot in this template, so the aerial is rendered to fit it."""
    try:
        slot = XlsmPatcher(template_bytes).find_image_slot(SHEET)
    except Exception:  # noqa: BLE001
        return None
    return slot.aspect if slot else None


# ---------------------------------------------------------------------------
# Keeping text inside its box
# ---------------------------------------------------------------------------
# A dig's directions, or a long alignment sheet name, can be longer than the box the
# template gives it, and Excel simply cuts the text off at the edge. One-line cells
# are set to shrink to fit; the wrapped Directions box gets the largest font that
# fits, worked out with Calibri's own metrics (Carlito, which is metric-compatible,
# when it is installed).

_CALIBRI_FILES = (
    "/usr/share/fonts/truetype/crosextra/Carlito-Regular.ttf",
    "/usr/share/fonts/truetype/crosextra/Carlito.ttf",
    "/usr/share/fonts/carlito/Carlito-Regular.ttf",
    "/usr/share/fonts/TTF/Carlito-Regular.ttf",
)
_CALIBRI = {"font": None, "tried": False}


def _text_width(text: str, size_pt: float) -> float:
    """Width in pixels (96 dpi) of ``text`` in Calibri at ``size_pt``."""
    pixels = size_pt * 96 / 72
    if not _CALIBRI["tried"]:
        _CALIBRI["tried"] = True
        for path in _CALIBRI_FILES:
            try:
                _CALIBRI["font"] = ImageFont.truetype(path, 100)
                break
            except OSError:
                continue
    font = _CALIBRI["font"]
    if font is not None:
        return font.getlength(text) * pixels / 100.0
    # No Carlito: Calibri averages about half an em a character; a little more than
    # that errs towards a smaller font rather than clipped text.
    return len(text) * pixels * 0.54


def wrapped_line_count(text: str, size_pt: float, width_px: float) -> int:
    lines = 0
    for paragraph in (text or "").split("\n"):
        current = ""
        count = 1
        for word in paragraph.split(" "):
            trial = f"{current} {word}" if current else word
            if _text_width(trial, size_pt) <= width_px:
                current = trial
                continue
            if current:
                count += 1
            overflow = _text_width(word, size_pt)
            while overflow > width_px:
                count += 1
                overflow -= width_px
            current = word if overflow <= width_px else ""
        lines += count
    return lines


def fit_font_size(text: str, width_px: float, height_pt: float,
                  base: float = 11.0, minimum: float = 6.0) -> float:
    """The largest font size, at most ``base``, at which wrapped text fits the box."""
    usable_width = max(width_px - 10, 20) / 1.04          # padding + a safety margin
    size = base
    while size > minimum:
        needed = wrapped_line_count(text, size, usable_width) * size * 1.37 + 4
        if needed <= height_pt:
            return size
        size -= 0.5
    return minimum


def fit_report_text(patcher: XlsmPatcher, layout: dict, sheet: str = SHEET,
                    overflow_name: bool = False) -> None:
    # A long "name + ODO" heading is left to run across the empty cells beside it
    # rather than shrunk into the one cell it starts in.
    shrink = [layout[f] for f in _SHRINK_FIELDS
              if f in layout and not (overflow_name and f == "dig_name")]
    patcher.restyle(sheet, shrink, shrink=True)
    for name in _WRAPPED_FIELDS:
        ref = layout.get(name)
        if not ref:
            continue
        text = patcher.cell_text(sheet, ref)
        box = patcher.box_size(sheet, ref)
        if not text or box is None:
            continue
        size = fit_font_size(text, box[0], box[1])
        if size < 11.0:
            patcher.restyle(sheet, [ref], size=size)


def _column_letters(index: int) -> str:
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def widen_merged_box(patcher: XlsmPatcher, sheet: str, ref: str, text: str,
                     texts: dict) -> str:
    """Widen the merged box that starts at ``ref`` until ``text`` fits on one line.

    The box grows evenly left and right across empty cells in its row and stops at
    any cell that holds text or belongs to another merged range, so the template's
    own labels are never covered. Returns the cell the text must now be written to
    (the box's new top-left), or ``ref`` when nothing needed to change.
    """
    path = patcher._sheet_paths.get(sheet)
    xml = patcher._text(path)
    match = re.search(rf'<mergeCell ref="{ref}:([A-Z]+)(\d+)"/>', xml)
    if not match or int(match.group(2)) != int(re.search(r"\d+", ref).group(0)):
        return ref
    row = int(match.group(2))
    first = _column_index(re.match(r"[A-Z]+", ref).group(0))
    last = _column_index(match.group(1))

    widths = {}
    for col in re.finditer(r"<col\b[^>]*/>", xml):
        tag = col.group(0)
        low, high = re.search(r'min="(\d+)"', tag), re.search(r'max="(\d+)"', tag)
        width = re.search(r'width="([\d.]+)"', tag)
        if low and high and width:
            for index in range(int(low.group(1)), min(int(high.group(1)), 60) + 1):
                widths[index] = float(width.group(1))
    px = lambda index: widths.get(index, 9.14) * 7  # noqa: E731

    needed = _text_width(text, 11) * 1.08 + 14          # bold, plus padding
    if sum(px(i) for i in range(first, last + 1)) >= needed:
        return ref

    other_merges = []
    for m in re.finditer(r'<mergeCell ref="([A-Z]+)(\d+):([A-Z]+)(\d+)"/>', xml):
        if m.group(1) + m.group(2) != ref and int(m.group(2)) <= row <= int(m.group(4)):
            other_merges.append((_column_index(m.group(1)), _column_index(m.group(3))))

    def free(index: int) -> bool:
        if index < 1 or f"{_column_letters(index)}{row}" in texts:
            return False
        return not any(lo <= index <= hi for lo, hi in other_merges)

    new_first, new_last = first, last
    while sum(px(i) for i in range(new_first, new_last + 1)) < needed:
        grew = False
        if free(new_first - 1):
            new_first -= 1
            grew = True
        if sum(px(i) for i in range(new_first, new_last + 1)) >= needed:
            break
        if free(new_last + 1):
            new_last += 1
            grew = True
        if not grew:
            break
    if (new_first, new_last) == (first, last):
        return ref

    new_ref = f"{_column_letters(new_first)}{row}"
    xml = xml.replace(match.group(0),
                      f'<mergeCell ref="{new_ref}:{_column_letters(new_last)}{row}"/>')
    # The text lives in the box's top-left cell, so the old top-left cell's style moves
    # to the new one and the old one takes the style of an interior cell of the box.
    old_style = re.search(rf'<c r="{ref}"[^>]*?\bs="(\d+)"', xml)
    new_cell = re.search(rf'<c r="{new_ref}"([^>]*?)(/?)>', xml)
    if old_style and new_cell:
        attrs = re.sub(r'\bs="\d+"', "", new_cell.group(1))
        xml = xml.replace(new_cell.group(0),
                          f'<c r="{new_ref}"{attrs} s="{old_style.group(1)}"{new_cell.group(2)}>', 1)
        interior = re.search(rf'<c r="{_column_letters(first + 1)}{row}"[^>]*?\bs="(\d+)"', xml)
        if interior:
            xml = re.sub(rf'(<c r="{ref}"[^>]*?\bs=")\d+(")', rf'\g<1>{interior.group(1)}\g<2>', xml, count=1)
    patcher.parts[path] = xml.encode("utf-8")
    return new_ref


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _feet_text(value) -> Optional[str]:
    return None if value is None else f"{value:,.2f}'"


def report_values(dig: Dig, settings: ReportSettings, info: Optional[TemplateInfo] = None) -> dict:
    """{field: value} - only what the uploads establish; Unknown where they do not."""
    info = info or TemplateInfo()
    odometer = _round2(dig.odometer)
    name = dig.name
    if info.name_has_odo and odometer is not None:
        name = f"{dig.name} ODO {odometer:.2f}"
    if info.detail_kind == "station":
        up_detail, down_detail = dig.us_ref_station or None, dig.ds_ref_station or None
    else:
        up_detail, down_detail = _round2(dig.us_ref_distance), _round2(dig.ds_ref_distance)
    values = {
        "dig_name": name,
        "up_ref": dig.upstream_reference or None,
        "up_feet": up_detail,
        "down_ref": dig.downstream_reference or None,
        "down_feet": down_detail,
        "up_gap": _feet_text(dig.us_ref_distance),
        "down_gap": _feet_text(dig.ds_ref_distance),
        "odometer": odometer,
        "station": None if dig.station_feet is None else int(round(dig.station_feet)),
        "alignment": dig.alignment_sheet or UNKNOWN,
        "tract": _numeric_if_possible(dig.tract_number or UNKNOWN),
        "legal": dig.legal_description or UNKNOWN,
        "county": dig.county or UNKNOWN,
        "state": dig.state or UNKNOWN,
        "hca": dig.hca_label,
        "directions": dig.directions or None,
        # The templates say "Leave blank if unknown": write blank, not the placeholder.
        "line_name": dig.line_name or "",
        "tool_type": dig.tool_type or "",
    }
    if settings.surveyor_name:
        values["surveyor"] = settings.surveyor_name
    return values


def _has_formula(patcher: XlsmPatcher, sheet: str, ref: str) -> bool:
    path = patcher._sheet_paths.get(sheet)
    if path is None:
        return False
    return re.search(rf'<c r="{ref}"[^>]*>\s*<f\b', patcher._text(path)) is not None


def build_staking_report(
    template_bytes: bytes,
    dig: Dig,
    settings: ReportSettings,
    info: Optional[TemplateInfo] = None,
) -> tuple:
    """Return (.xlsm bytes, warnings) for a single dig."""
    patcher = XlsmPatcher(template_bytes)
    warnings = []
    if SHEET not in patcher.sheet_names():
        raise ValueError(
            f"The template has no '{SHEET}' sheet - found {', '.join(patcher.sheet_names())}.")

    info = info or read_template_info(template_bytes)
    layout = info.layout
    for problem in info.problems:
        if problem not in warnings:
            warnings.append(problem)

    values = report_values(dig, settings, info)
    layout = dict(layout)
    if "dig_name" in layout and values.get("dig_name"):
        layout["dig_name"] = widen_merged_box(
            patcher, SHEET, layout["dig_name"], values["dig_name"], sheet_texts(patcher, SHEET))
    to_write = {}
    for name, value in values.items():
        if value is None or name not in layout:
            continue
        to_write[layout[name]] = value
    unplaced = patcher.set_values(SHEET, to_write)
    if unplaced:
        warnings.append("Could not place these cells in the template: " + ", ".join(sorted(unplaced)))

    # The phone number is a lookup keyed off the surveyor's name. It is not written;
    # only the value it last showed is refreshed so previews that do not recalculate
    # still read correctly.
    if settings.surveyor_name and "surveyor" in layout and info.phones:
        phone_ref = f"C{int(layout['surveyor'][1:]) + 1}"
        if _has_formula(patcher, SHEET, phone_ref):
            patcher.set_cached(SHEET, {phone_ref: info.phones.get(settings.surveyor_name, "")})

    fit_report_text(patcher, layout)
    patcher.force_full_recalc()

    if dig.aerial_image:
        slot = patcher.find_image_slot(SHEET)
        if slot is None:
            warnings.append(
                "No image placeholder found on the template's StakingReport sheet, "
                "so the aerial image was not inserted.")
        else:
            patcher.replace_image(slot, _encode_for_slot(dig.aerial_image, slot), "jpeg")

    return patcher.to_bytes(), warnings


def _encode_for_slot(image_bytes: bytes, slot) -> bytes:
    """Match the slot's aspect so Excel does not stretch the image."""
    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    target_aspect = slot.aspect
    width, height = image.size
    if abs((width / height) - target_aspect) > 0.005:
        if width / height > target_aspect:
            new_width = int(round(height * target_aspect))
            left = (width - new_width) // 2
            image = image.crop((left, 0, left + new_width, height))
        else:
            new_height = int(round(width / target_aspect))
            top = (height - new_height) // 2
            image = image.crop((0, top, width, top + new_height))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90, subsampling=0, optimize=True)
    return buffer.getvalue()


# ==========================================================================
# GENERATION
# ==========================================================================
# The whole run, kept out of the UI so the tests can exercise it.

XLSM_MIME = "application/vnd.ms-excel.sheet.macroEnabled.12"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _file_part(text: str) -> str:
    """A name made safe to use inside a file name."""
    return re.sub(r'[\\/:*?"<>|]+', "-", str(text or "")).strip() or "Unknown"


def staking_report_name(dig: Dig) -> str:
    return f"Staking Reports/{_file_part(dig.label)} - {_file_part(dig.name)}_Staking Report.xlsm"


def cheat_sheet_name(group: tuple, several: bool) -> str:
    if not several:
        return "Dig Stake Cheat Sheet.xlsx"
    pipeline, ili = group
    return f"Dig Stake Cheat Sheet - {_file_part(pipeline)} {_file_part(ili)}.xlsx".replace("  ", " ")


@dataclass
class RunOptions:
    make_aerial: bool = True
    basemap: str = "Esri World Imagery"
    span_feet: Optional[float] = None          # None = automatic (dig plus nearest AGM)
    make_directions: bool = False
    token: str = ""
    auto_notes: bool = True
    full_cheat_names: bool = False
    station_method: str = "interpolate"


def lookup_county_state(digs: list, token: str = "") -> list:
    """Fill county and state from GPS where they are still Unknown. Returns problems."""
    problems, failures, first_error = [], 0, ""
    for dig in digs:
        if dig.latitude is None or dig.longitude is None:
            continue
        if dig.county not in ("", UNKNOWN) and dig.state not in ("", UNKNOWN):
            continue
        try:
            dig.county, dig.state = county_and_state(dig.latitude, dig.longitude, token)
        except Exception as error:  # noqa: BLE001
            failures += 1
            first_error = first_error or str(error)
    if failures:
        problems.append(
            f"County/state could not be looked up for {failures} dig(s) and was left "
            f"Unknown - fill it in on the table. ({first_error})")
    return problems


def generate_files(
    digs: list,
    template_bytes: bytes,
    cheat_templates: dict,
    kmz: Optional[KmzData],
    settings: ReportSettings,
    options: RunOptions,
    progress=None,
) -> tuple:
    """Return ({zip path: bytes}, [issues]) for the given digs."""
    outputs, issues = {}, []
    info = read_template_info(template_bytes)
    slot_aspect = info.slot_aspect
    total = max(1, len(digs))

    def tick(fraction, text):
        if progress is not None:
            progress(min(max(fraction, 0.0), 1.0), text)

    for dig in digs:
        dig.station_method = options.station_method

    for index, dig in enumerate(digs, start=1):
        tick((index - 0.8) / total, f"{dig.label or dig.name} - aerial image")
        dig.aerial_image = None
        if options.make_aerial:
            try:
                dig.aerial_image = render_aerial(
                    dig, kmz, basemap=options.basemap, span_feet=options.span_feet,
                    token=options.token, slot_aspect=slot_aspect, others=digs)
                if dig.aerial_image is None and dig.latitude is not None:
                    issues.append(f"{dig.label}: aerial image could not be rendered.")
            except Exception as error:  # noqa: BLE001 - surfaced to the user
                issues.append(f"{dig.label}: aerial image failed - {error}")

        if options.make_directions and options.token and dig.latitude is not None:
            tick((index - 0.45) / total, f"{dig.label or dig.name} - directions")
            try:
                dig.directions = generate_directions(
                    float(dig.latitude), float(dig.longitude), options.token).paragraph
            except Exception as error:  # noqa: BLE001
                issues.append(f"{dig.label}: directions failed - {error}")

        tick((index - 0.15) / total, f"{dig.label or dig.name} - staking report")
        try:
            report, report_warnings = build_staking_report(template_bytes, dig, settings, info)
            outputs[staking_report_name(dig)] = report
            seen = set()
            for warning in report_warnings:
                if warning not in seen:
                    seen.add(warning)
                    issues.append(f"{dig.label}: {warning}")
        except Exception as error:  # noqa: BLE001
            issues.append(f"{dig.label}: staking report failed - {error}")

    # One cheat sheet per pipeline and ILI range: odometers are only comparable
    # within one run.
    groups: dict = {}
    for dig in digs:
        groups.setdefault(dig.group, []).append(dig)
    for group, members in groups.items():
        template = pick_cheat_template(cheat_templates, members)
        if template is None:
            issues.append("No cheat sheet template uploaded, so no cheat sheet was made.")
            break
        try:
            outputs[cheat_sheet_name(group, len(groups) > 1)] = build_cheat_sheet(
                template, members, auto_notes=options.auto_notes,
                full_names=options.full_cheat_names)
        except Exception as error:  # noqa: BLE001
            issues.append(f"Cheat sheet failed - {error}")

    tick(1.0, "Done")
    return outputs, issues


# ==========================================================================
# STREAMLIT UI
# ==========================================================================

def main() -> None:
    import streamlit as st

    st.set_page_config(page_title="Entegra Dig File Generator", page_icon="🛠️",
                       layout="wide")

    for key in ("digs", "kmz_data", "outputs", "template_info", "read_notes", "zip_path"):
        st.session_state.setdefault(key, None)

    def mapbox_token() -> str:
        try:
            return st.secrets["mapbox"]["token"]
        except Exception:  # noqa: BLE001
            return ""

    token = mapbox_token()

    st.title("Entegra Dig File Generator")
    st.caption(
        "Fills a staking report for every dig sheet PDF (one per replacement), plus one cheat sheet, with "
        "driving directions and an aerial image in each report. Latitude, longitude, "
        "elevation, EDOC, survey date and photos are field measurements and are left blank."
    )

    left, right = st.columns(2)
    with left:
        template_file = st.file_uploader(
            "Staking report template (.xlsm)", type=["xlsm"], key="template",
            help="Your dig stake template. It holds your crew names, so upload it each run; it is not stored in the repo.")
        cheat_files = st.file_uploader(
            "Cheat sheet template(s) (.xlsx)", type=["xlsx"], accept_multiple_files=True,
            key="cheat",
            help="Upload the ascending one, the descending one, or both. With both, the one "
                 "matching the dig sheets' stationing is used.")
    with right:
        dig_files = st.file_uploader(
            "Dig sheets (.pdf) and the assessment summary", type=["pdf"],
            accept_multiple_files=True, key="digs_pdf",
            help="The Entegra dig sheet PDFs. The line name is taken from each file's name. "
                 "Include the assessment summary PDF - the tool type comes from it.")
        kmz_file = st.file_uploader(
            "Pipeline KMZ (aerial image, county and state)", type=["kmz", "kml"], key="kmz")

    template_info: Optional[TemplateInfo] = None
    if template_file is not None:
        template_info = read_template_info(template_file.getvalue())
        for problem in template_info.problems:
            st.warning(f"Staking report template: {problem}")

    st.sidebar.header("Report details")
    stakers = template_info.stakers if template_info else []
    if stakers:
        choice = st.sidebar.selectbox(
            "Surveyed by", ["(leave blank)"] + stakers, index=0,
            help="The phone number fills itself in from the template's Contacts table.")
        surveyor_name = "" if choice == "(leave blank)" else choice
    else:
        surveyor_name = st.sidebar.text_input("Surveyed by", value="")

    st.sidebar.header("County and state")
    look_up_county = st.sidebar.checkbox(
        "Look up from the dig's GPS coordinates", value=True,
        help="Uses the FCC and Census geocoders (and Mapbox as a fallback).")

    st.sidebar.header("Aerial image")
    make_aerial = st.sidebar.checkbox("Generate aerial images", value=True)
    basemap = st.sidebar.selectbox("Imagery", basemap_options(bool(token)), index=0)
    auto_span = st.sidebar.radio(
        "Image width", ["Automatic (dig and nearest AGM)", "Fixed"], index=0)
    span_feet = None
    if auto_span == "Fixed":
        span_feet = float(st.sidebar.slider("Ground width (ft)", 800, 6000, 2600, 200))

    st.sidebar.header("Directions")
    make_directions = st.sidebar.checkbox(
        "Generate driving directions", value=bool(token), disabled=not token,
        help="Same Mapbox logic as the Dig Site Directions Generator. Needs a token in "
             ".streamlit/secrets.toml.")
    if not token:
        st.sidebar.caption("No Mapbox token found - directions will be left blank.")

    st.sidebar.header("Stationing")
    station_choice = st.sidebar.radio(
        "Station of each dig",
        ["Between the two reference stations", "U/S reference station + odometer offset"],
        index=0,
        help="The dig sheet prints stations only for the U/S and D/S references. The first "
             "option spreads the distance in proportion to the odometer, so it stays on the "
             "stationing's own scale even when stationing and odometer do not agree.")
    station_method = "interpolate" if station_choice.startswith("Between") else "offset"

    st.sidebar.header("Cheat sheet")
    auto_notes = st.sidebar.checkbox("Auto-note digs close to the previous one", value=True)
    full_names = st.sidebar.checkbox(
        "Use the full dig name in the cheat sheet", value=False,
        help="Off: 'Dig 4'. On: the whole heading, which is far wider than the column.")

    ready = bool(template_file and cheat_files and dig_files)
    if not ready:
        st.info("Upload the staking report template, a cheat sheet template and the dig "
                "sheet PDFs to begin. The KMZ is needed for the aerial image.")

    if st.button("Read uploads", type="primary", disabled=not ready):
        with st.status("Reading dig sheets...", expanded=False):
            digs, tool_type, problems = parse_dig_pdfs(
                [(upload.name, upload.getvalue()) for upload in dig_files])
            if digs and not tool_type:
                problems.append("No assessment line was found, so the tool type is blank "
                                "in the reports. Upload the assessment summary PDF.")
            kmz = None
            if kmz_file is not None:
                try:
                    kmz = load_kmz(kmz_file.getvalue())
                except Exception as error:  # noqa: BLE001
                    problems.append(f"{kmz_file.name}: {error}")
            if kmz is not None:
                for dig in digs:
                    check_dig_against_kmz(dig, kmz)
            elif make_aerial:
                problems.append("No KMZ loaded, so the aerial images will show the dig "
                                "but no pipe or AGMs.")
            if look_up_county:
                problems.extend(lookup_county_state(digs, token))

        st.session_state.digs = digs
        st.session_state.kmz_data = kmz
        st.session_state.outputs = None
        st.session_state.zip_path = None
        st.session_state.read_notes = problems

    for problem in st.session_state.read_notes or []:
        st.warning(problem)

    digs = st.session_state.digs or []
    if not digs:
        _downloads(st)
        return

    # ------------------------------------------------------------------
    # Review
    # ------------------------------------------------------------------
    st.subheader(f"{len({d.source_file for d in digs})} dig sheets read")
    st.caption("One dig per dig sheet PDF (REP number). Nothing is taken from the KMZ except the map. Untick Include to leave one out.")
    st.caption("Alignment sheet, tract, legal description and HCA stay Unknown unless you "
               "fill them in here. County and state were looked up from the GPS "
               "coordinates; correct them here if needed.")

    for dig in digs:
        dig.station_method = station_method
    edited = st.data_editor(
        [
            {
                "Include": dig.primary,
                "Dig": dig.name,
                "ODO": _round2(dig.odometer),
                "Station": dig.station_label,
                "U/S ref": dig.upstream_reference,
                "U/S ft": _round2(dig.us_ref_distance),
                "D/S ref": dig.downstream_reference,
                "D/S ft": _round2(dig.ds_ref_distance),
                "Latitude": dig.latitude,
                "Longitude": dig.longitude,
                "County": dig.county,
                "State": dig.state,
                "Alignment sheet": dig.alignment_sheet,
                "Tract": dig.tract_number,
                "Legal description": dig.legal_description,
                "HCA": dig.hca if dig.hca in ("Yes", "No") else UNKNOWN,
                "Notes": dig.notes,
            }
            for dig in digs
        ],
        width="stretch",
        num_rows="fixed",
        key="review",
        disabled=["Dig", "ODO", "Station", "U/S ref", "U/S ft", "D/S ref", "D/S ft",
                  "Latitude", "Longitude"],
        column_config={
            "HCA": st.column_config.SelectboxColumn("HCA", options=[UNKNOWN, "Yes", "No"]),
            "Notes": st.column_config.TextColumn(
                "Cheat sheet notes", help="Goes in the Notes column of the cheat sheet."),
        },
    )
    included = []
    for row, dig in zip(edited, digs):
        if row.get("Include", True):
            included.append(dig)
        dig.county = _text(row.get("County")) or UNKNOWN
        dig.state = _text(row.get("State")) or UNKNOWN
        dig.alignment_sheet = _text(row.get("Alignment sheet")) or UNKNOWN
        dig.tract_number = _text(row.get("Tract")) or UNKNOWN
        dig.legal_description = _text(row.get("Legal description")) or UNKNOWN
        dig.hca = _text(row.get("HCA")) or UNKNOWN
        dig.notes = _text(row.get("Notes"))

    warnings = [(d.label or d.name, w) for d in digs for w in d.warnings]
    if warnings:
        with st.expander(f"{len(warnings)} thing(s) to check", expanded=len(warnings) <= 12):
            for label, warning in warnings:
                st.write(f"**{label}** - {warning}")

    if st.button(f"Generate files for {len(included)} dig{'s' if len(included) != 1 else ''}",
                 type="primary", disabled=not included):
        cheat_templates = {}
        for upload in cheat_files:
            cheat_templates.setdefault(cheat_sheet_flavour(upload.getvalue()), upload.getvalue())
        options = RunOptions(
            make_aerial=make_aerial, basemap=basemap, span_feet=span_feet,
            make_directions=make_directions, token=token, auto_notes=auto_notes,
            full_cheat_names=full_names, station_method=station_method)
        bar = st.progress(0.0, text="Starting...")
        outputs, issues = generate_files(
            included, template_file.getvalue(), cheat_templates, st.session_state.kmz_data,
            ReportSettings(surveyor_name=surveyor_name), options,
            progress=lambda fraction, text: bar.progress(fraction, text=text))
        bar.empty()
        st.session_state.outputs = outputs
        st.session_state.zip_path = None
        for issue in issues:
            st.warning(issue)

    _downloads(st)


def write_zip(files: dict, handle) -> None:
    with zipfile.ZipFile(handle, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(files):
            archive.writestr(name, files[name])


def _downloads(st) -> None:
    outputs = st.session_state.outputs or {}
    if not outputs:
        return
    st.subheader("Files")
    path = st.session_state.get("zip_path")
    if not path or not os.path.exists(path):
        handle = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        with handle:
            write_zip(outputs, handle)
        path = st.session_state.zip_path = handle.name
    megabytes = os.path.getsize(path) / 1_000_000
    with open(path, "rb") as zipped:
        st.download_button(
            f"Download everything (.zip, {megabytes:,.1f} MB)", data=zipped,
            file_name=f"Entegra Dig Files {date.today():%Y-%m-%d}.zip",
            mime="application/zip", type="primary")
    with st.expander(f"{len(outputs)} files in the zip", expanded=False):
        for name in sorted(outputs):
            st.text(name)
    previews = [d for d in (st.session_state.digs or []) if d.aerial_image]
    if previews:
        with st.expander("Aerial images", expanded=False):
            for dig in previews:
                st.image(dig.aerial_image, caption=dig.name, width="stretch")


if __name__ == "__main__":
    main()
