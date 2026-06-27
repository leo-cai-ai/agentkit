from agentkit.core.memory.tokenizer import HeuristicTokenEstimator


def test_empty_is_zero():
    est = HeuristicTokenEstimator()
    assert est.estimate("") == 0


def test_ascii_roughly_chars_over_four():
    est = HeuristicTokenEstimator(chars_per_token=4.0)
    # 16 ascii chars -> ceil(16/4) == 4
    assert est.estimate("a" * 16) == 4


def test_cjk_weighted_heavier():
    est = HeuristicTokenEstimator(cjk_tokens_per_char=1.5)
    # 4 CJK chars -> ceil(4 * 1.5) == 6
    assert est.estimate("你好世界") == 6


def test_monotonic_non_decreasing():
    est = HeuristicTokenEstimator()
    short = est.estimate("hello world")
    longer = est.estimate("hello world " * 10)
    assert longer >= short


def test_mixed_text_counts_both():
    est = HeuristicTokenEstimator(chars_per_token=4.0, cjk_tokens_per_char=1.5)
    # "你好" (2 CJK -> 3) + "abcd" (4 ascii -> 1) = 4
    assert est.estimate("你好abcd") == 4


def test_invalid_params_rejected():
    for kwargs in ({"chars_per_token": 0}, {"cjk_tokens_per_char": -1}):
        raised = False
        try:
            HeuristicTokenEstimator(**kwargs)
        except ValueError:
            raised = True
        assert raised
