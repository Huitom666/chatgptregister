"""Offline checks against the installed curl_cffi request/native configuration.

No test calls Curl.perform or contacts an HTTP server. Native libcurl may accept
an alias which Session's option preparation rejects, so native acceptance alone
is insufficient: the application catalog must use canonical target names too.
"""
import random
import re

import pytest

import fingerprint


TARGETS = sorted(fingerprint._ALL_IMPERSONATES)
FAMILIES = tuple(fingerprint._GENERATORS)


def _assert_matching_ua(fp, target):
    entry = fingerprint._ALL_IMPERSONATES[target]
    profile = entry["data"]
    assert fp["browser_type"] == entry["type"]
    ua = fp["user_agent"]
    if entry["type"] in {"mac_safari", "ios_safari"}:
        assert f"Version/{profile['safari_ver']} " in ua
        assert ("iPhone" in ua) == (entry["type"] == "ios_safari")
        assert ("Macintosh" in ua) == (entry["type"] == "mac_safari")
        assert fp["sec_ch_ua"] == ""
    elif entry["type"] == "chrome":
        assert f"Chrome/{profile['full_ver']} " in ua
        assert f'"Chromium";v="{profile["ver"]}"' in fp["sec_ch_ua"]
        assert f'v="{profile["full_ver"]}"' in fp["sec_ch_ua_full_version_list"]
    else:
        assert f"Firefox/{profile['ver']}" in ua
        assert f"rv:{profile['ver']}" in ua
        assert fp["sec_ch_ua"] == ""


def test_catalog_uses_canonical_targets_not_deprecated_safari_aliases():
    from curl_cffi.requests.impersonate import BrowserType

    declared = {target.value for target in BrowserType}
    assert set(TARGETS) <= declared
    assert not any(re.match(r"safari\d+_\d", target) for target in TARGETS)
    assert {"safari180", "safari180_ios", "safari184_ios"} <= set(TARGETS)


@pytest.mark.parametrize("target", TARGETS)
def test_every_configured_target_is_accepted_before_network_io(target, monkeypatch):
    from curl_cffi import Curl
    from curl_cffi.requests.utils import set_curl_options

    def forbid_network(*args, **kwargs):
        raise AssertionError("Compatibility validation must not perform a request")

    monkeypatch.setattr(Curl, "perform", forbid_network)
    curl = Curl()
    try:
        assert curl.impersonate(target, default_headers=False) == 0
        curl.reset()
        # Exercise the same option setup used by Session.request, without perform.
        set_curl_options(
            curl, "GET", "https://offline.invalid/", impersonate=target,
            params_list=[None, None], headers_list=[None, None],
            cookies_list=[None, None], proxies_list=[None, None],
            verify_list=[True, None],
        )
    finally:
        curl.close()


def test_generated_primary_and_fallback_targets_cover_only_supported_catalog():
    seen = set()
    for seed in range(500):
        fp = fingerprint.generate_fingerprint(random.Random(seed), country_code="US")
        primary = fp["impersonate"]
        fallbacks = fp["fallback_impersonates"]
        assert primary == fallbacks[0]
        assert len(fallbacks) == len(set(fallbacks))
        assert set(fallbacks) <= set(TARGETS)
        _assert_matching_ua(fp, primary)
        for target in fallbacks:
            assert fingerprint._ALL_IMPERSONATES[target]["type"] == fp["browser_type"]
        seen.add(primary)
    # Ensure the random-generation regression check actually visits every profile.
    assert seen == set(TARGETS)


@pytest.mark.parametrize("family", FAMILIES)
def test_same_family_fallback_keeps_device_and_updates_version_fields(family):
    fp = next(
        fp for seed in range(100)
        if (fp := fingerprint.generate_fingerprint(random.Random(seed), "JP"))["browser_type"] == family
    )
    for target in fp["fallback_impersonates"]:
        updated = fingerprint.fingerprint_for_impersonate(target, fp)
        assert updated["impersonate"] == target
        _assert_matching_ua(updated, target)
        for key in (
            "screen", "lang", "lang_full", "timezone", "navigator_platform",
            "navigator_vendor", "hardware_concurrency", "device_memory",
            "max_touch_points", "device_pixel_ratio",
        ):
            assert updated[key] == fp[key]
        assert updated is not fp
