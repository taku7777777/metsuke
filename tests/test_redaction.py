from metsuke.redaction import redact


def test_redacts_common_secrets_without_leaking_plaintext():
    text = (
        "use key sk-ant-api03-" + "a" * 40 + " and token ghp_" + "b" * 36
        + " plus AKIAABCDEFGHIJKLMNOP ok"
    )
    out, detections = redact(text)
    assert "sk-ant-" not in out
    assert "ghp_" not in out
    assert "AKIA" not in out
    assert "[REDACTED:anthropic_key:" in out
    assert len(detections) == 3
    # detections carry name+hash only, never plaintext
    assert all(len(d.split(":")[1]) == 12 for d in detections)


def test_plain_text_untouched():
    text = "普通のプロンプト。コストの説明をしてください。ghpという文字列は無害。"
    out, detections = redact(text)
    assert out == text and detections == []


def test_stage6_secret_patterns():
    values = [
        "sk-proj-" + "a" * 32,
        "AWS_SECRET_ACCESS_KEY=" + "b" * 40,
        "xoxc-" + "c" * 20,
        "AIza" + "d" * 35,
        "glpat-" + "e" * 24,
        "npm_" + "f" * 24,
        "sk_live_" + "g" * 24,
        "hf_" + "h" * 24,
        "-----BEGIN PRIVATE KEY-----\n" + "i" * 10_000 + "\n-----END PRIVATE KEY-----",
    ]
    out, detections = redact("\n".join(values))
    assert len(detections) == len(values)
    assert all(value not in out for value in values)
