# Prompt Versioning

Prompts are versioned assets, separate from application code. Store one prompt per workflow under a named directory, for example:

```text
prompts/customer_360/v1/system.txt
prompts/customer_360/v1/user.txt
```

Prompt loading, variable substitution, and schema selection belong in the AI layer. Prompts must not contain secrets or tenant-specific data.
