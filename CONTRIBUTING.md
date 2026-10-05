# Contributing

Use synthetic messages and temporary databases. Run `python -m pytest -q`.
Keep sender-account checks, recipient identity checks, pause behavior,
contact-isolated history, per-page authentication and duplicate-send protection.

New client versions require source review and actual Windows validation. Do not
claim a mock test proves a successful WeChat send. Record observed behavior,
test environment and limitations without user data.

Do not add license bypasses, login interception, code injection, hidden network
protocols, account farming or bulk unsolicited messaging. Prefer documented
interfaces and narrowly reviewed compatibility changes. Include provenance and
compatible licensing when incorporating third-party source.

CI template: ci/windows-tests.yml. To enable it, copy it into .github/workflows/tests.yml using a GitHub credential with workflow permission or the repository web editor.
