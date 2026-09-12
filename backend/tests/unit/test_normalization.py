from app.ingestion.normalization import normalize_text


def test_collapses_excess_blank_lines():
    text = "Para one.\n\n\n\n\nPara two."
    assert normalize_text(text) == "Para one.\n\nPara two."


def test_strips_null_and_control_characters():
    text = "Hello\x00World\x01!"
    assert normalize_text(text) == "HelloWorld!"


def test_normalizes_windows_and_mac_line_endings():
    assert normalize_text("a\r\nb\rc") == "a\nb\nc"


def test_collapses_repeated_spaces():
    assert normalize_text("too    many     spaces") == "too many spaces"


def test_strips_leading_and_trailing_whitespace():
    assert normalize_text("   padded text   \n\n") == "padded text"


def test_nfkc_normalizes_compatibility_characters():
    # U+00A0 (non-breaking space) folds to a regular space under NFKC.
    text = "no break"
    assert normalize_text(text) == "no break"


def test_preserves_meaningful_punctuation_and_case():
    text = "Section 4.2: Do NOT modify this clause!"
    assert normalize_text(text) == text


def test_empty_input_returns_empty_string():
    assert normalize_text("") == ""
    assert normalize_text("   \n\n  ") == ""
