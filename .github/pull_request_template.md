## Summary

<!-- What does this change do, in plain language? -->

## Testing

<!-- What did you run to prove it works? Paste the commands and their results. -->
<!-- The CI runs hassfest, HACS validation, and the smoke tests; a feature is only merged when all of them pass. -->

- [ ] `python3 test_bridge.py`
- [ ] `python3 test_sensor_device_info.py`
- [ ] Verified against a real Home Assistant (note the version) or marked N/A with a reason

## Checklist

- [ ] One feature only — no "miscellaneous" changes bundled in
- [ ] No secrets, credentials, or personal data added
- [ ] `CHANGELOG.md` updated if user-visible behaviour changed
- [ ] Manifest `version` bumped if this is a release (never bump it in a feature branch unless the feature is the release)
