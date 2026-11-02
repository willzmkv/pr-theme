#!/usr/bin/env python3
"""Static validator for the Project-R ES-DE theme (stdlib only).

It re-implements the checks that make ES-DE 3.5.0's ThemeData parser abort a theme
(unknown element/property, blank value, malformed pair/colour, undeclared variant or
aspect ratio, missing explicit include) plus checks of our own:

* every .xml and .svg file in the theme is well-formed XML
* every explicitly referenced asset path exists (ES-DE only logs a warning for those)
* ``${variables}`` used by the theme are defined
* capabilities.xml declares at least one variant
* the status badge assets and badges/manifest.json match the pr-meta contract

The theme is "loaded" once per (system, variant, aspect ratio) combination, following
the parsing order documented in THEMES.md ("Configuration parsing order").

Usage: python tools/validate_theme.py [THEME_DIR]
"""

from __future__ import annotations

import json
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = TOOLS_DIR / "esde_schema_3.5.0.json"
DEFAULT_THEME_DIR = TOOLS_DIR.parent

# Directories that are part of the repository but not of the theme.
IGNORED_DIRS = {".git", ".github", ".pytest_cache", ".preview", "__pycache__", "tests", "tools"}

# Systems to simulate: (name, theme folder, full name, kind). The five standalone-emulator
# systems are required by the Project-R design spec; the rest cover RetroArch systems and
# ES-DE's automatic/custom collections, which all fall back to the root theme.xml.
SYSTEMS = [
    ("ps3", "ps3", "PlayStation 3", "system"),
    ("xbox360", "xbox360", "Xbox 360", "system"),
    ("ps4", "ps4", "PlayStation 4", "system"),
    ("switch", "switch", "Nintendo Switch", "system"),
    ("ps5", "ps5", "PlayStation 5", "system"),
    ("n64", "n64", "Nintendo 64", "system"),
    ("psx", "psx", "Sony PlayStation", "system"),
    ("all", "all", "all games", "auto"),
    ("favorites", "favorites", "favorites", "auto"),
    ("lastplayed", "lastplayed", "last played", "auto"),
    ("collections", "collections", "collections", "custom"),
]
STANDALONE_SYSTEMS = ["ps3", "xbox360", "ps4", "switch", "ps5"]

TOP_LEVEL_TAGS = {
    "include",
    "variables",
    "view",
    "variant",
    "aspectRatio",
    "colorScheme",
    "fontSize",
    "language",
    "transitions",
}
FORBIDDEN_TOP_LEVEL_TAGS = {"formatVersion", "feature"}
NAME_DELIMS = re.compile(r"[\s,]+")
VARIABLE = re.compile(r"\$\{([^}]*)\}")
HEX_COLOR = re.compile(r"^[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$")
FLOAT = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")

IMAGE_TYPES = {
    "image": {"image", "miximage", "marquee", "screenshot", "titlescreen", "cover",
              "backcover", "3dbox", "physicalmedia", "fanart"},
    "video": {"image", "miximage", "marquee", "screenshot", "titlescreen", "cover",
              "backcover", "3dbox", "physicalmedia", "fanart"},
    "carousel": {"marquee", "cover", "backcover", "3dbox", "physicalmedia", "screenshot",
                 "titlescreen", "miximage", "fanart", "none"},
    "grid": {"marquee", "cover", "backcover", "3dbox", "physicalmedia", "screenshot",
             "titlescreen", "miximage", "fanart", "none"},
}

# pr-meta badge contract (see README "Badge contract").
BADGE_STATES = {
    "NATIVE": ("badges/native.svg", "#4ecdc4"),
    "EMU": ("badges/emu.svg", "#f5a623"),
    "BUILDING": ("badges/building.svg", "#4a9eff"),
    "BUILD ERROR": ("badges/build-error.svg", "#e94560"),
}
BADGE_OVERRIDE_DIR = "~/ES-DE/pr-badges"
BADGE_ELEMENT = "prStatusBadge"


def split_names(value: str) -> list[str]:
    return [part for part in NAME_DELIMS.split(value) if part]


def load_schema(path: Path = SCHEMA_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass
class Capabilities:
    variants: list[str] = field(default_factory=list)
    aspect_ratios: list[str] = field(default_factory=list)
    color_schemes: list[str] = field(default_factory=list)
    font_sizes: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    files_parsed: set[Path] = field(default_factory=set)
    assets_checked: set[Path] = field(default_factory=set)
    combinations: int = 0
    # (system, variant, aspect) -> view -> element key -> {property: value}
    resolved: dict = field(default_factory=dict)

    def error(self, message: str) -> None:
        if message not in self.errors:
            self.errors.append(message)

    def note(self, message: str) -> None:
        if message not in self.notes:
            self.notes.append(message)


class ThemeLoader:
    """Mimics ThemeData::loadFile for one system / variant / aspect ratio."""

    def __init__(self, theme_dir: Path, schema: dict, caps: Capabilities, report: Report,
                 system: tuple[str, str, str, str], variant: str, aspect: str) -> None:
        self.theme_dir = theme_dir
        self.schema = schema
        self.caps = caps
        self.report = report
        self.variant = variant
        self.aspect = aspect
        name, theme_folder, full_name, kind = system
        self.system = name
        self.variables: dict[str, str] = system_variables(name, theme_folder, full_name, kind)
        self.views: dict[str, dict[str, dict[str, str]]] = {}
        self.stack: list[Path] = []

    # -- helpers -----------------------------------------------------------------------
    def where(self) -> str:
        rel = self.stack[-1].relative_to(self.theme_dir).as_posix() if self.stack else "?"
        return f"{rel} [system={self.system} variant={self.variant} aspect={self.aspect}]"

    def resolve(self, text: str, context: str) -> str:
        def substitute(match: re.Match) -> str:
            key = match.group(1)
            if key not in self.variables:
                self.report.error(f"{self.where()}: undefined variable ${{{key}}} in {context}")
                return ""
            return self.variables[key]

        # Variables may nest (a variable's value may contain another variable).
        for _ in range(10):
            new = VARIABLE.sub(substitute, text)
            if new == text:
                break
            text = new
        return text

    def resolve_path(self, value: str) -> Path | None:
        """Return the filesystem path ES-DE would use, or None for runtime-only paths."""
        if value.startswith(":/"):
            return None  # ES-DE bundled resource
        if value.startswith("~/"):
            return None  # user's home at runtime (e.g. the pr-meta badge directory)
        if value.startswith("./"):
            return (self.stack[-1].parent / value[2:]).resolve()
        return Path(value)

    # -- parsing -----------------------------------------------------------------------
    def load(self, path: Path) -> None:
        self.stack.append(path)
        root = self.parse(path)
        if root is not None:
            if root.tag != "theme":
                self.report.error(f"{self.where()}: missing <theme> root tag")
            else:
                self.parse_theme_node(root, top_level=True)
        self.stack.pop()

    def parse(self, path: Path) -> ET.Element | None:
        try:
            tree = ET.parse(path)
        except ET.ParseError as exc:
            self.report.error(f"{path.relative_to(self.theme_dir).as_posix()}: XML error: {exc}")
            return None
        self.report.files_parsed.add(path)
        return tree.getroot()

    def parse_theme_node(self, node: ET.Element, top_level: bool) -> None:
        for child in node:
            if not isinstance(child.tag, str):
                continue
            if top_level and child.tag in FORBIDDEN_TOP_LEVEL_TAGS:
                self.report.error(f"{self.where()}: unsupported <{child.tag}> tag")
            elif child.tag not in TOP_LEVEL_TAGS:
                self.report.error(f"{self.where()}: unknown tag <{child.tag}> under <theme>")
        # Same order as ThemeData::loadFile / parseIncludes / parseVariants.
        for child in node.findall("variables"):
            for var in child:
                if not isinstance(var.tag, str):
                    continue
                value = self.resolve(var.text or "", f"variable {var.tag}").strip()
                if value:
                    self.variables[var.tag] = value
        for child in node.findall("include"):
            self.include(child)
        for child in node.findall("view"):
            self.view(child)
        for child in node.findall("variant"):
            names = self.declared_names(child, "variant", self.caps.variants + ["all"])
            if self.variant in names or "all" in names:
                self.parse_theme_node(child, top_level=False)
        for child in node.findall("aspectRatio"):
            names = self.declared_names(child, "aspectRatio", self.caps.aspect_ratios)
            if self.aspect in names:
                self.parse_theme_node(child, top_level=False)

    def declared_names(self, node: ET.Element, tag: str, declared: list[str]) -> list[str]:
        raw = node.get("name")
        if raw is None:
            self.report.error(f"{self.where()}: <{tag}> tag missing name attribute")
            return []
        names = split_names(raw)
        for name in names:
            if name not in declared:
                self.report.error(
                    f"{self.where()}: <{tag}> value '{name}' is not defined in capabilities.xml"
                )
        return names

    def include(self, node: ET.Element) -> None:
        raw = (node.text or "").strip()
        value = self.resolve(raw, "<include>")
        uses_variable = value != raw
        target = self.resolve_path(value) if value else None
        if target is None or not target.is_file():
            if uses_variable:
                self.report.note(f"{self.where()}: optional include '{raw}' not found (skipped)")
            else:
                self.report.error(f"{self.where()}: include '{raw}' not found")
            return
        self.load(target)

    def view(self, node: ET.Element) -> None:
        raw = node.get("name")
        if raw is None:
            self.report.error(f"{self.where()}: <view> missing name attribute")
            return
        for view_name in split_names(raw):
            if view_name not in self.schema["supportedViews"]:
                self.report.error(f"{self.where()}: unsupported view '{view_name}'")
                continue
            elements = self.views.setdefault(view_name, {})
            for element in node:
                if isinstance(element.tag, str):
                    self.element(view_name, element, elements)

    def element(self, view_name: str, node: ET.Element, elements: dict) -> None:
        etype = node.tag
        props_schema = self.schema["elements"].get(etype)
        if props_schema is None:
            self.report.error(f"{self.where()}: unknown element type <{etype}>")
            return
        raw_name = node.get("name")
        if raw_name is None:
            self.report.error(f"{self.where()}: <{etype}> missing name attribute")
            return
        if node.get("extra") is not None:
            self.report.error(f"{self.where()}: unsupported 'extra' attribute on <{etype}>")
        attrs = self.schema["propertyAttributes"].get(etype, {})
        for elem_name in split_names(raw_name):
            props = elements.setdefault(f"{etype}_{elem_name}", {})
            for prop in node:
                if not isinstance(prop.tag, str):
                    continue
                ptype = props_schema.get(prop.tag)
                context = f"<{etype} name=\"{elem_name}\"> property <{prop.tag}>"
                if ptype is None:
                    self.report.error(f"{self.where()}: unknown property {context}")
                    continue
                key = prop.tag
                if prop.tag in attrs:
                    attr_value = prop.get(attrs[prop.tag])
                    if attr_value is None:
                        self.report.error(
                            f"{self.where()}: {context} needs attribute '{attrs[prop.tag]}'"
                        )
                        continue
                    key = f"{attrs[prop.tag]}_{attr_value}"
                raw = (prop.text or "").strip()
                value = self.resolve(raw, context).strip()
                if value == "\b":
                    continue  # mutually exclusive system variable that does not apply
                if not value:
                    self.report.error(f"{self.where()}: {context} has no value")
                    continue
                self.check_value(etype, prop.tag, ptype, raw, value, context)
                props[key] = value

    def check_value(self, etype: str, prop: str, ptype: str, raw: str, value: str,
                    context: str) -> None:
        where = self.where()
        if ptype == "NORMALIZED_PAIR":
            parts = value.split()
            if len(parts) != 2 or not all(FLOAT.match(p) for p in parts):
                self.report.error(f"{where}: {context} invalid normalized pair '{value}'")
        elif ptype == "COLOR":
            if not HEX_COLOR.match(value):
                self.report.error(f"{where}: {context} invalid colour '{value}' (RRGGBB[AA])")
        elif ptype == "FLOAT":
            if not FLOAT.match(value):
                self.report.error(f"{where}: {context} invalid float '{value}'")
        elif ptype == "UNSIGNED_INTEGER":
            if not value.isdigit():
                self.report.error(f"{where}: {context} invalid unsigned integer '{value}'")
        elif ptype == "BOOLEAN":
            if value not in ("true", "false", "1", "0"):
                self.report.error(f"{where}: {context} invalid boolean '{value}'")
        elif ptype == "PATH":
            self.check_path(prop, raw, value, context)
        if prop == "imageType" and etype in IMAGE_TYPES:
            types = split_names(value)
            invalid = [t for t in types if t not in IMAGE_TYPES[etype]]
            if invalid:
                self.report.error(f"{where}: {context} invalid media type(s) {invalid}")
            if len(set(types)) != len(types):
                self.report.error(f"{where}: {context} duplicate media types")

    def check_path(self, prop: str, raw: str, value: str, context: str) -> None:
        if not (value.startswith(("./", "~/", ":/")) or Path(value).is_absolute()):
            self.report.error(
                f"{self.where()}: {context} path '{raw}' must start with ./ (theme-relative) or ~/"
            )
            return
        target = self.resolve_path(value)
        if target is None:
            return
        try:
            target.relative_to(self.theme_dir)
        except ValueError:
            self.report.error(f"{self.where()}: {context} path '{raw}' points outside the theme")
            return
        expect_dir = prop == "gameOverridePath"
        exists = target.is_dir() if expect_dir else target.is_file()
        if exists:
            self.report.assets_checked.add(target)
        elif value != raw:
            # Set through a variable: ES-DE only logs a debug message and falls back.
            self.report.note(f"{self.where()}: optional asset '{raw}' -> '{value}' not found")
        else:
            self.report.error(f"{self.where()}: {context} asset '{raw}' not found")


def system_variables(name: str, theme_folder: str, full_name: str, kind: str) -> dict[str, str]:
    """System variables as populated by SystemData::loadTheme (THEMES.md "System variables")."""
    variables = {}
    for var, value in (("system.name", name), ("system.fullName", full_name),
                       ("system.theme", theme_folder)):
        variables[var] = value
        for suffix, applies in (("autoCollections", kind == "auto"),
                                ("customCollections", kind == "custom"),
                                ("noCollections", kind == "system")):
            # ES-DE marks non-applicable variants with "\b" and skips those properties.
            variables[f"{var}.{suffix}"] = value if applies else "\b"
    return variables


def parse_capabilities(theme_dir: Path, schema: dict, report: Report) -> Capabilities | None:
    caps_path = theme_dir / "capabilities.xml"
    if not caps_path.is_file():
        report.error("capabilities.xml is missing (ES-DE will not load the theme)")
        return None
    try:
        root = ET.parse(caps_path).getroot()
    except ET.ParseError as exc:
        report.error(f"capabilities.xml: XML error: {exc}")
        return None
    report.files_parsed.add(caps_path)
    if root.tag != "themeCapabilities":
        report.error("capabilities.xml: missing <themeCapabilities> root tag")
        return None
    caps = Capabilities()
    for node in root.findall("aspectRatio"):
        value = (node.text or "").strip()
        if value not in schema["supportedAspectRatios"] or value == "automatic":
            report.error(f"capabilities.xml: unsupported aspect ratio '{value}'")
        elif value in caps.aspect_ratios:
            report.error(f"capabilities.xml: duplicate aspect ratio '{value}'")
        else:
            caps.aspect_ratios.append(value)
    for node in root.findall("fontSize"):
        value = (node.text or "").strip()
        if value not in schema["supportedFontSizes"]:
            report.error(f"capabilities.xml: unsupported font size '{value}'")
        else:
            caps.font_sizes.append(value)
    for tag, target in (("variant", caps.variants), ("colorScheme", caps.color_schemes)):
        for node in root.findall(tag):
            name = node.get("name", "").strip()
            if not name:
                report.error(f"capabilities.xml: <{tag}> missing name attribute")
            elif name == "all":
                report.error(f"capabilities.xml: '{name}' is a reserved {tag} name")
            elif name in target:
                report.error(f"capabilities.xml: duplicate {tag} '{name}'")
            else:
                target.append(name)
            for label in node.findall("label"):
                if not (label.text or "").strip():
                    report.error(f"capabilities.xml: empty <label> for {tag} '{name}'")
    for node in root.findall("language"):
        caps.languages.append((node.text or "").strip())
    if not caps.variants:
        report.error("capabilities.xml: no <variant> declared (Project-R requires at least one)")
    return caps


def check_well_formed(theme_dir: Path, report: Report) -> None:
    for path in sorted(theme_dir.rglob("*")):
        rel_parts = path.relative_to(theme_dir).parts
        if any(part in IGNORED_DIRS for part in rel_parts):
            continue
        if path.suffix.lower() in (".xml", ".svg") and path.is_file():
            try:
                ET.parse(path)
            except ET.ParseError as exc:
                report.error(f"{path.relative_to(theme_dir).as_posix()}: XML error: {exc}")


def check_svg_assets(theme_dir: Path, report: Report) -> None:
    """ES-DE renders SVG with LunaSVG: keep files self-contained and free of <text>."""
    svg_ns = "{http://www.w3.org/2000/svg}"
    for path in sorted(theme_dir.rglob("*.svg")):
        rel = path.relative_to(theme_dir)
        if any(part in IGNORED_DIRS for part in rel.parts):
            continue
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            continue  # already reported by check_well_formed
        if root.tag != f"{svg_ns}svg":
            report.error(f"{rel.as_posix()}: root element is not an SVG <svg>")
            continue
        for attr in ("width", "height", "viewBox"):
            if root.get(attr) is None:
                report.error(f"{rel.as_posix()}: <svg> lacks {attr}")
        for node in root.iter():
            tag = node.tag.replace(svg_ns, "") if isinstance(node.tag, str) else ""
            if tag in ("text", "image", "style", "foreignObject", "script"):
                report.error(f"{rel.as_posix()}: <{tag}> is not allowed (outline text instead)")
            for key, val in node.attrib.items():
                if key.endswith("href") and not val.startswith("#"):
                    report.error(f"{rel.as_posix()}: external reference '{val}' not allowed")


def first_rect_fill(path: Path) -> str | None:
    root = ET.parse(path).getroot()
    rect = root.find("{http://www.w3.org/2000/svg}rect")
    return None if rect is None else rect.get("fill")


def check_badges(theme_dir: Path, report: Report) -> None:
    manifest_path = theme_dir / "badges" / "manifest.json"
    if not manifest_path.is_file():
        report.error("badges/manifest.json is missing")
        return
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("overrideDir") != BADGE_OVERRIDE_DIR:
        report.error(f"badges/manifest.json: overrideDir must be {BADGE_OVERRIDE_DIR}")
    states = manifest.get("states", {})
    if set(states) != set(BADGE_STATES):
        report.error(f"badges/manifest.json: states must be exactly {sorted(BADGE_STATES)}")
    for state, (rel, color) in BADGE_STATES.items():
        entry = states.get(state, {})
        if entry.get("file") != rel or entry.get("color") != color:
            report.error(f"badges/manifest.json: {state} must map to {rel} / {color}")
        path = theme_dir / rel
        if not path.is_file():
            report.error(f"{rel}: badge asset missing")
            continue
        if first_rect_fill(path) != color:
            report.error(f"{rel}: badge background must be {color}")
    pattern = manifest.get("buildingProgressFile", "")
    if pattern != "badges/building/{progress:03d}.svg":
        report.error("badges/manifest.json: unexpected buildingProgressFile pattern")
        return
    for progress in range(0, 101):
        rel = pattern.format(progress=progress)
        path = theme_dir / rel
        if not path.is_file():
            report.error(f"{rel}: progress badge missing")
        elif first_rect_fill(path) != BADGE_STATES["BUILDING"][1]:
            report.error(f"{rel}: progress badge background must be BUILDING blue")


def check_font_licenses(theme_dir: Path, report: Report) -> None:
    for font in sorted(theme_dir.rglob("*.[ot]tf")):
        rel = font.relative_to(theme_dir)
        if any(part in IGNORED_DIRS for part in rel.parts):
            continue
        if not any((font.parent / name).is_file() for name in ("OFL.txt", "LICENSE.txt", "LICENSE")):
            report.error(f"{rel.as_posix()}: font without a licence file next to it")


def check_contract(theme_dir: Path, report: Report) -> None:
    """The gamelist view of every simulated system must carry the badge element."""
    for (system, variant, aspect), views in report.resolved.items():
        badge = views.get("gamelist", {}).get(f"image_{BADGE_ELEMENT}")
        label = f"[system={system} variant={variant} aspect={aspect}]"
        if badge is None:
            report.error(f"{label}: gamelist view lacks image '{BADGE_ELEMENT}'")
            continue
        if badge.get("gameOverridePath") != BADGE_OVERRIDE_DIR:
            report.error(f"{label}: {BADGE_ELEMENT} gameOverridePath must be {BADGE_OVERRIDE_DIR}")
        if "imageType" in badge or "path" in badge:
            report.error(f"{label}: {BADGE_ELEMENT} must not define imageType or path")


def validate(theme_dir: Path = DEFAULT_THEME_DIR, schema: dict | None = None,
             systems: list[tuple[str, str, str, str]] | None = None) -> Report:
    theme_dir = theme_dir.resolve()
    schema = schema or load_schema()
    report = Report()
    check_well_formed(theme_dir, report)
    caps = parse_capabilities(theme_dir, schema, report)
    if caps is not None:
        variants = caps.variants or [""]
        aspects = caps.aspect_ratios or [""]
        for system in systems or SYSTEMS:
            theme_file = theme_dir / system[1] / "theme.xml"
            if not theme_file.is_file():
                theme_file = theme_dir / "theme.xml"
            if not theme_file.is_file():
                report.error(f"no theme.xml for system '{system[0]}' (and no root theme.xml)")
                continue
            for variant in variants:
                for aspect in aspects:
                    loader = ThemeLoader(theme_dir, schema, caps, report, system, variant, aspect)
                    loader.load(theme_file)
                    report.resolved[(system[0], variant, aspect)] = loader.views
                    report.combinations += 1
                    for view in ("system", "gamelist"):
                        if view not in loader.views:
                            report.error(
                                f"[system={system[0]} variant={variant} aspect={aspect}]: "
                                f"no '{view}' view defined"
                            )
        check_contract(theme_dir, report)
    check_svg_assets(theme_dir, report)
    check_badges(theme_dir, report)
    check_font_licenses(theme_dir, report)
    return report


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    theme_dir = Path(args[0]) if args else DEFAULT_THEME_DIR
    report = validate(theme_dir)
    for note in report.notes:
        print(f"note: {note}")
    for error in report.errors:
        print(f"ERROR: {error}", file=sys.stderr)
    status = "FAILED" if report.errors else "OK"
    print(
        f"{status}: {len(report.files_parsed)} theme XML files, "
        f"{len(report.assets_checked)} asset paths verified, "
        f"{report.combinations} system/variant/aspect-ratio combinations, "
        f"{len(report.errors)} error(s)"
    )
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
