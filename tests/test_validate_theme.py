"""Tests for tools/validate_theme.py against the real theme and broken copies of it."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest
import validate_theme as vt

ROOT = Path(__file__).resolve().parent.parent


def copy_theme(tmp_path: Path) -> Path:
    target = tmp_path / "project-r"
    shutil.copytree(
        ROOT,
        target,
        ignore=shutil.ignore_patterns(".git", ".github", "tests", "tools", "__pycache__",
                                      ".pytest_cache", "*.ttf"),
    )
    # Fonts are large binaries; validation only needs them to exist.
    for font in (ROOT / "core" / "fonts").glob("*.ttf"):
        (target / "core" / "fonts" / font.name).write_bytes(b"")
    return target


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{old!r} not found in {path}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def errors_matching(report: vt.Report, pattern: str) -> list[str]:
    return [e for e in report.errors if re.search(pattern, e)]


@pytest.fixture(scope="module")
def report() -> vt.Report:
    return vt.validate(ROOT)


# -- the real theme ------------------------------------------------------------------------


def test_repository_theme_has_no_errors(report: vt.Report) -> None:
    assert report.errors == []


def test_every_system_variant_and_aspect_ratio_is_simulated(report: vt.Report) -> None:
    assert report.combinations == len(vt.SYSTEMS) * 2 * 3
    assert {"withVideos", "coversOnly"} == {variant for _, variant, _ in report.resolved}
    assert {"16:9", "16:10", "4:3"} == {aspect for _, _, aspect in report.resolved}


def test_all_theme_files_are_parsed(report: vt.Report) -> None:
    parsed = {p.relative_to(ROOT).as_posix() for p in report.files_parsed}
    assert parsed == {
        "capabilities.xml",
        "theme.xml",
        "core/variables.xml",
        "core/common.xml",
        "core/system.xml",
        "core/gamelist-view.xml",
        "core/aspect-ratios.xml",
    }


@pytest.mark.parametrize("system", vt.STANDALONE_SYSTEMS)
def test_standalone_systems_render_with_wordmark(report: vt.Report, system: str) -> None:
    views = report.resolved[(system, "withVideos", "16:9")]
    assert {"system", "gamelist"} <= set(views)
    static = views["system"]["carousel_systemCarousel"]["staticImage"]
    assert static == f"./../systems/logos/{system}.svg"
    assert (ROOT / "core" / static).resolve().is_file()


def test_other_systems_fall_back_to_text_without_error(report: vt.Report) -> None:
    assert not (ROOT / "systems" / "logos" / "n64.svg").exists()
    assert any("systems/logos/n64.svg" in note for note in report.notes)


@pytest.mark.parametrize("combo", sorted(vt.validate(ROOT).resolved))
def test_badge_element_uses_game_override_path(report: vt.Report, combo: tuple) -> None:
    badge = report.resolved[combo]["gamelist"]["image_prStatusBadge"]
    assert badge["gameOverridePath"] == "~/ES-DE/pr-badges"
    assert "path" not in badge and "imageType" not in badge
    assert badge["metadataElement"] == "true"


def test_cli_reports_ok(capsys: pytest.CaptureFixture[str]) -> None:
    assert vt.main([str(ROOT)]) == 0
    assert capsys.readouterr().out.strip().splitlines()[-1].startswith("OK: 7 theme XML files")


# -- broken copies must be rejected --------------------------------------------------------


def test_copied_theme_is_valid(tmp_path: Path) -> None:
    assert vt.validate(copy_theme(tmp_path)).errors == []


def test_malformed_xml_is_reported(tmp_path: Path) -> None:
    theme = copy_theme(tmp_path)
    edit(theme / "core" / "system.xml", "</carousel>", "</carousle>")
    assert errors_matching(vt.validate(theme), r"core/system\.xml: XML error")


def test_malformed_svg_is_reported(tmp_path: Path) -> None:
    theme = copy_theme(tmp_path)
    edit(theme / "systems" / "logos" / "ps3.svg", "</svg>", "")
    assert errors_matching(vt.validate(theme), r"systems/logos/ps3\.svg: XML error")


def test_missing_explicit_asset_is_reported(tmp_path: Path) -> None:
    theme = copy_theme(tmp_path)
    (theme / "badges" / "emu.svg").unlink()
    report = vt.validate(theme)
    assert errors_matching(report, r"asset '\./\.\./badges/emu\.svg' not found")
    assert errors_matching(report, r"badges/emu\.svg: badge asset missing")


def test_missing_font_is_reported(tmp_path: Path) -> None:
    theme = copy_theme(tmp_path)
    (theme / "core" / "fonts" / "JetBrainsMono-Bold.ttf").unlink()
    assert errors_matching(vt.validate(theme), r"JetBrainsMono-Bold\.ttf' not found")


def test_missing_include_is_reported(tmp_path: Path) -> None:
    theme = copy_theme(tmp_path)
    (theme / "core" / "aspect-ratios.xml").unlink()
    assert errors_matching(vt.validate(theme), r"include '\./core/aspect-ratios\.xml' not found")


def test_optional_variable_include_is_not_an_error(tmp_path: Path) -> None:
    theme = copy_theme(tmp_path)
    edit(theme / "theme.xml", "</theme>", "<include>./${system.theme}/extra.xml</include></theme>")
    report = vt.validate(theme)
    assert report.errors == []
    assert any("optional include" in note for note in report.notes)


def test_unknown_property_is_reported(tmp_path: Path) -> None:
    theme = copy_theme(tmp_path)
    edit(theme / "core" / "gamelist-view.xml", "<metadataElement>true</metadataElement>\n"
         "            <zIndex>45</zIndex>", "<zIndex>45</zIndex><imageTypo>x</imageTypo>")
    assert errors_matching(vt.validate(theme), r"unknown property .*<imageTypo>")


def test_unknown_element_is_reported(tmp_path: Path) -> None:
    theme = copy_theme(tmp_path)
    edit(theme / "core" / "system.xml", "<view name=\"system\">",
         "<view name=\"system\"><banner name=\"x\"><pos>0 0</pos></banner>")
    assert errors_matching(vt.validate(theme), r"unknown element type <banner>")


def test_unsupported_view_is_reported(tmp_path: Path) -> None:
    theme = copy_theme(tmp_path)
    edit(theme / "core" / "system.xml", "<view name=\"system\">", "<view name=\"system, detailed\">")
    assert errors_matching(vt.validate(theme), r"unsupported view 'detailed'")


def test_undefined_variable_is_reported(tmp_path: Path) -> None:
    theme = copy_theme(tmp_path)
    edit(theme / "core" / "system.xml", "${prTextMuted}", "${prTextMutedTypo}")
    assert errors_matching(vt.validate(theme), r"undefined variable \$\{prTextMutedTypo\}")


def test_invalid_colour_is_reported(tmp_path: Path) -> None:
    theme = copy_theme(tmp_path)
    edit(theme / "core" / "variables.xml", "<prSurface>0D0D1A</prSurface>",
         "<prSurface>#0D0D1A</prSurface>")
    assert errors_matching(vt.validate(theme), r"invalid colour '#0D0D1A'")


def test_invalid_normalized_pair_is_reported(tmp_path: Path) -> None:
    theme = copy_theme(tmp_path)
    edit(theme / "core" / "system.xml", "<pos>0.5 0.72</pos>", "<pos>0.5</pos>")
    assert errors_matching(vt.validate(theme), r"invalid normalized pair '0\.5'")


def test_blank_value_is_reported(tmp_path: Path) -> None:
    theme = copy_theme(tmp_path)
    edit(theme / "core" / "system.xml", "<zIndex>40</zIndex>", "<zIndex> </zIndex>")
    assert errors_matching(vt.validate(theme), r"<zIndex> has no value")


def test_invalid_media_type_is_reported(tmp_path: Path) -> None:
    theme = copy_theme(tmp_path)
    edit(theme / "core" / "gamelist-view.xml", "cover, 3dbox", "covr, 3dbox")
    assert errors_matching(vt.validate(theme), r"invalid media type\(s\) \['covr'\]")


def test_undeclared_variant_is_reported(tmp_path: Path) -> None:
    theme = copy_theme(tmp_path)
    edit(theme / "core" / "gamelist-view.xml", "<variant name=\"coversOnly\">",
         "<variant name=\"coversOnly, ghost\">")
    assert errors_matching(vt.validate(theme), r"<variant> value 'ghost' is not defined")


def test_undeclared_aspect_ratio_is_reported(tmp_path: Path) -> None:
    theme = copy_theme(tmp_path)
    edit(theme / "core" / "aspect-ratios.xml", "<aspectRatio name=\"4:3\">",
         "<aspectRatio name=\"4:3, 21:9\">")
    assert errors_matching(vt.validate(theme), r"<aspectRatio> value '21:9' is not defined")


def test_capabilities_without_variant_is_reported(tmp_path: Path) -> None:
    theme = copy_theme(tmp_path)
    caps = theme / "capabilities.xml"
    text = caps.read_text(encoding="utf-8")
    caps.write_text(re.sub(r"<variant .*?</variant>", "", text, flags=re.S), encoding="utf-8")
    report = vt.validate(theme)
    assert errors_matching(report, r"no <variant> declared")
    # The theme's <variant> blocks now reference undeclared names as well.
    assert errors_matching(report, r"<variant> value 'withVideos' is not defined")


def test_missing_capabilities_is_reported(tmp_path: Path) -> None:
    theme = copy_theme(tmp_path)
    (theme / "capabilities.xml").unlink()
    assert errors_matching(vt.validate(theme), r"capabilities\.xml is missing")


def test_invalid_capabilities_aspect_ratio_is_reported(tmp_path: Path) -> None:
    theme = copy_theme(tmp_path)
    edit(theme / "capabilities.xml", "<aspectRatio>16:10</aspectRatio>",
         "<aspectRatio>16:11</aspectRatio>")
    assert errors_matching(vt.validate(theme), r"unsupported aspect ratio '16:11'")


def test_badge_element_without_override_is_reported(tmp_path: Path) -> None:
    theme = copy_theme(tmp_path)
    edit(theme / "core" / "gamelist-view.xml",
         "<gameOverridePath>${prBadgeDir}</gameOverridePath>", "")
    assert errors_matching(vt.validate(theme), r"prStatusBadge gameOverridePath must be")


def test_wrong_badge_colour_is_reported(tmp_path: Path) -> None:
    theme = copy_theme(tmp_path)
    edit(theme / "badges" / "build-error.svg", 'fill="#e94560"', 'fill="#ff0000"')
    assert errors_matching(vt.validate(theme), r"build-error\.svg: badge background must be")


def test_missing_progress_badge_is_reported(tmp_path: Path) -> None:
    theme = copy_theme(tmp_path)
    (theme / "badges" / "building" / "057.svg").unlink()
    assert errors_matching(vt.validate(theme), r"badges/building/057\.svg: progress badge missing")


def test_svg_text_element_is_rejected(tmp_path: Path) -> None:
    theme = copy_theme(tmp_path)
    edit(theme / "badges" / "native.svg", "</svg>", "<text>NATIVE</text></svg>")
    assert errors_matching(vt.validate(theme), r"native\.svg: <text> is not allowed")


def test_path_outside_theme_is_rejected(tmp_path: Path) -> None:
    theme = copy_theme(tmp_path)
    edit(theme / "core" / "system.xml", "./../badges/native.svg", "./../../native.svg")
    assert errors_matching(vt.validate(theme), r"points outside the theme")


def test_font_without_licence_is_reported(tmp_path: Path) -> None:
    theme = copy_theme(tmp_path)
    (theme / "core" / "fonts" / "OFL.txt").unlink()
    assert errors_matching(vt.validate(theme), r"font without a licence file")
