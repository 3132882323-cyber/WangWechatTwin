# Privacy

Public source contains no user's API keys, OAuth tokens, WeChat database keys,
contact lists, chat records, databases, screenshots, runtime logs, personal
business notes or derived personal style corpus.

Local connection and processing must be limited to accounts and devices the
operator owns or is authorized to control. Encryption keys remain local under
restricted Windows filesystem permissions. The original WeChat files are read
only; decoding and WAL application operate on snapshots.

Ordinary model requests can send the current conversation's necessary text and
selected same-contact style examples to the configured provider. Detected
credentials and critical sensitive cases stay local. This is not a promise that
all possible sensitive information can be detected. Review your provider,
configuration, retention policy and data access before enabling automation.

Do not publish `.env`, `config.yaml`, local connection configs, `.runtime`,
backups, source chat exports, logs or `data/learned_style_summary.json`.
Before sharing an issue or screenshot, remove contact identifiers, private
messages and credentials. If a credential has already been exposed, rotate it
at the issuing service; deleting a Git commit alone is not enough.
