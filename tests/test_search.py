"""search.cosine_topk の純粋関数テスト。matrix/query は正規化済み前提。"""
from __future__ import annotations

import numpy as np

from shelf.search import build_fts_query, cosine_topk, rrf_merge


def test_ranks_closest_vector_first():
    ids = ["a", "b", "c"]
    matrix = np.array(
        [
            [1.0, 0.0],   # a: query と直交 → score 0
            [0.0, 1.0],   # b: query と同方向 → score 1
            [0.0, -1.0],  # c: query と逆方向 → score -1
        ],
        dtype=np.float32,
    )
    query = np.array([0.0, 1.0], dtype=np.float32)

    hits = cosine_topk(matrix, ids, query, limit=3)

    assert [h.id for h in hits] == ["b", "a", "c"]


def test_scores_are_in_descending_order():
    ids = ["a", "b", "c"]
    matrix = np.array([[0.0, 1.0], [0.6, 0.8], [1.0, 0.0]], dtype=np.float32)
    query = np.array([0.0, 1.0], dtype=np.float32)

    hits = cosine_topk(matrix, ids, query, limit=3)

    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)


def test_respects_limit():
    ids = ["a", "b", "c"]
    matrix = np.array([[0.0, 1.0], [0.6, 0.8], [1.0, 0.0]], dtype=np.float32)
    query = np.array([0.0, 1.0], dtype=np.float32)

    hits = cosine_topk(matrix, ids, query, limit=1)

    assert len(hits) == 1
    assert hits[0].id == "a"


def test_returns_empty_list_for_empty_matrix():
    ids: list[str] = []
    matrix = np.zeros((0, 0), dtype=np.float32)
    query = np.array([0.0, 1.0], dtype=np.float32)

    hits = cosine_topk(matrix, ids, query, limit=5)

    assert hits == []


# -- rrf_merge: 複数の順位付きidリストを Reciprocal Rank Fusion で統合する ------


def test_rrf_merge_fuses_two_rankings_by_hand_computed_scores():
    # k=1で手計算: a = 1/(1+1) + 1/(1+2) = 0.5 + 0.3333.. = 0.8333..
    #              b = 1/(1+2) + 1/(1+3) = 0.3333.. + 0.25   = 0.5833..
    #              c = 1/(1+3) + 1/(1+1) = 0.25    + 0.5    = 0.75
    # 降順: a(0.8333) > c(0.75) > b(0.5833)
    rankings = [["a", "b", "c"], ["c", "a", "b"]]

    merged = rrf_merge(rankings, limit=3, k=1)

    assert merged == ["a", "c", "b"]


def test_rrf_merge_breaks_ties_by_first_appearance_order():
    # k=1で手計算: p = 1/(1+1)[list0] + 1/(1+2)[list1] = 0.8333..
    #              q = 1/(1+2)[list0] + 1/(1+1)[list1] = 0.8333..(pと完全同点)
    # 同点は最初に登場した順(list0走査時にpが先、qが後)で決定的にpが先。
    rankings = [["p", "q"], ["q", "p"]]

    merged = rrf_merge(rankings, limit=2, k=1)

    assert merged == ["p", "q"]


def test_rrf_merge_handles_one_empty_ranking():
    rankings: list[list[str]] = [[], ["a", "b"]]

    merged = rrf_merge(rankings, limit=2)

    assert merged == ["a", "b"]


def test_rrf_merge_clamps_to_limit():
    rankings = [["a", "b", "c", "d"]]

    merged = rrf_merge(rankings, limit=2)

    assert merged == ["a", "b"]


def test_rrf_merge_returns_empty_list_for_no_rankings():
    assert rrf_merge([], limit=5) == []


def test_rrf_merge_returns_empty_list_when_all_rankings_are_empty():
    assert rrf_merge([[], []], limit=5) == []


# -- build_fts_query: 質問文をFTS5 MATCH用に無害化する ---------------------------


def test_build_fts_query_quotes_each_word_and_ors_them():
    result = build_fts_query("whales eat krill")

    assert result == '"whales" OR "eat" OR "krill"'


def test_build_fts_query_treats_double_quote_as_a_run_boundary():
    # 記号(ここでは`"`)はラン境界なので、'quote"mark' は "quote" と "mark" の
    # 2つの独立したASCIIランに分かれる(旧実装の空白分割と異なる新仕様)。
    result = build_fts_query('quote"mark test')

    assert result == '"quote" OR "mark" OR "test"'


def test_build_fts_query_literalizes_fts_operator_keywords_without_syntax_error():
    # AND/NEARはFTS5の演算子だが、各語をquoteで囲むためリテラル語として
    # 扱われ、構文エラーにはならない(呼び出し側のstore.keyword_topkが安全に使える)。
    # `*`は記号(ラン境界)なので"NEAR*"は"NEAR"として残る。"OR"は2文字でtrigram
    # では照合不能なため捨てられる(下記の短ラン破棄テストと同じ規則)。
    result = build_fts_query("AND OR NEAR* test")

    assert result == '"AND" OR "NEAR" OR "test"'


def test_build_fts_query_returns_empty_string_for_blank_input():
    assert build_fts_query("") == ""
    assert build_fts_query("   ") == ""


# -- build_fts_query: 日本語自然文のCJKトライグラム展開(コードレビュー指摘#1) -------


def test_build_fts_query_expands_cjk_run_into_sliding_trigrams():
    # 非ASCIIラン「量子力学」(4文字)をstride1の3文字グラムに展開する: 2グラム。
    result = build_fts_query("量子力学")

    assert result == '"量子力" OR "子力学"'


def test_build_fts_query_discards_cjk_run_shorter_than_three_chars():
    # 2文字のCJKランはtrigramで照合不能なため捨てられ、全タームが捨てられた
    # ケースとして空文字を返す(既存の空劣化パスに乗る)。
    assert build_fts_query("量子") == ""


def test_build_fts_query_discards_ascii_run_shorter_than_three_chars():
    result = build_fts_query("ab cd efg")

    assert result == '"efg"'


def test_build_fts_query_mixes_ascii_words_and_cjk_trigrams():
    # ASCIIラン(Rust/code)はそのまま1タームずつ、CJKラン(量子力学)は
    # スライディングウィンドウの3文字グラムに展開され、両方がOR結合される。
    result = build_fts_query("Rust code 量子力学")

    assert result == '"Rust" OR "code" OR "量子力" OR "子力学"'


def test_build_fts_query_handles_natural_japanese_sentence_without_spaces():
    # スペースなしの日本語自然文でも、CJKランがまるごと1フレーズにならず
    # 複数の3文字グラムに展開される(不具合の再現条件そのもの)。
    result = build_fts_query("量子力学の基礎について教えてください")

    assert '"量子力"' in result
    assert '"力学の"' in result
    assert " OR " in result


def test_build_fts_query_deduplicates_terms_keeping_first_occurrence_order():
    result = build_fts_query("quantum quantum classical")

    assert result == '"quantum" OR "classical"'


def test_build_fts_query_caps_total_term_count_via_even_stride_sampling():
    # コードレビュー指摘#7: 先頭32件への単純切り詰めだと長い質問文の後半が
    # 一切FTSタームに反映されない。均等ストライドで全体をカバーすることを検証する。
    words = [f"w{i:03d}" for i in range(40)]  # 40個のユニークな3文字以上ASCII語
    question = " ".join(words)

    result = build_fts_query(question)

    terms = result.split(" OR ")
    # n=40, k=32 の均等ストライド添字（先頭・末尾を必ず含む）: 手計算値。
    expected_indices = [
        0, 1, 3, 4, 5, 6, 8, 9, 10, 11, 13, 14, 15, 16, 18, 19,
        20, 21, 23, 24, 25, 26, 28, 29, 30, 31, 33, 34, 35, 36, 38, 39,
    ]
    assert len(terms) == 32
    assert terms == [f'"{words[i]}"' for i in expected_indices]
    # 旧実装（先頭32件切り詰め）なら含まれ得なかった後半由来のタームが
    # 含まれていることを示す（単純truncationとの差分を明示的に検証）。
    assert any(int(t[2:-1]) >= 32 for t in terms)


def test_build_fts_query_even_stride_covers_head_middle_and_tail_of_long_cjk_question():
    # 長い日本語質問文でも先頭付近だけでなく文全体からタームが選ばれることを
    # 検証する。トライグラムの位置を一意に特定できるよう、CJK統一漢字を
    # 1文字ずつ変えた合成文字列（意味を持たない）を使う。
    question = "".join(chr(0x4E00 + i) for i in range(50))  # 50文字の非ASCIIラン → 48トライグラム

    result = build_fts_query(question)

    terms = result.split(" OR ")
    assert len(terms) == 32
    all_trigrams = [question[i : i + 3] for i in range(len(question) - 2)]
    first_term = f'"{all_trigrams[0]}"'
    last_term = f'"{all_trigrams[-1]}"'
    assert terms[0] == first_term
    assert terms[-1] == last_term
    # 中間（トライグラム添字 20〜27付近）由来のタームも含まれる＝先頭偏重の
    # 切り詰めでは絶対に出てこない範囲がカバーされていることの確認。
    middle_terms = {f'"{t}"' for t in all_trigrams[20:28]}
    assert middle_terms & set(terms)


def test_build_fts_query_unchanged_when_under_cap():
    # 質問文が短くタームがcapを超えない場合は、ストライド間引きを経由せず
    # 従来通り全タームがそのまま出現順で使われる。
    result = build_fts_query("quantum classical mechanics")

    assert result == '"quantum" OR "classical" OR "mechanics"'
