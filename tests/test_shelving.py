"""shelving.py の純粋関数テスト（自動分類投入の分類判断・設計書 §13）。

FileSummary/NotebookCard/ClassificationDecision（ports.py）の手組み fixture のみを使い、
外部 CLI・ネットワーク・DB・backend には一切触れない（設計書 §13.9）。同一入力から
常に同一出力が得られること（決定論）を前提に、classify_step の全分岐
（assign 実在／assign 幻覚→create／new 正常／new 不正名→正規化／new 衝突→連番／
this-run 重複／description の created spec への伝播／working カタログ成長）を
手組み fixture のみで網羅する（設計書 §13.10 V5 done-criteria）。
"""
from __future__ import annotations

from shelf.ports import ClassificationDecision, FileSummary, NotebookCard
from shelf.shelving import (
    CLASSIFY_SCHEMA,
    StepResult,
    build_classification_prompt,
    classify_step,
    parse_classification,
)


def _card(**overrides) -> NotebookCard:
    base = dict(name="physics", description="物理学の教材集", persona=None, doc_count=3)
    base.update(overrides)
    return NotebookCard(**base)


def _summary(**overrides) -> FileSummary:
    base = dict(origin="/abs/dir/note.md", summary="ニュートン力学の講義ノート")
    base.update(overrides)
    return FileSummary(**base)


def _decision(**overrides) -> ClassificationDecision:
    base = dict(action="assign", notebook="physics", reason="主題が一致", parse_ok=True)
    base.update(overrides)
    return ClassificationDecision(**base)


class TestBuildClassificationPrompt:
    def test_includes_file_summary_text(self):
        prompt = build_classification_prompt(_summary(summary="量子力学の講義ノート"), [_card()])

        assert "量子力学の講義ノート" in prompt

    def test_includes_notebook_name_and_doc_count(self):
        card = _card(name="physics", doc_count=7)

        prompt = build_classification_prompt(_summary(), [card])

        assert "physics" in prompt
        assert "7" in prompt

    def test_includes_description_when_present(self):
        card = _card(description="物理学の教材集")

        prompt = build_classification_prompt(_summary(), [card])

        assert "物理学の教材集" in prompt

    def test_omits_description_when_none(self):
        card = _card(description=None)

        prompt = build_classification_prompt(_summary(), [card])

        assert "概要:" not in prompt

    def test_includes_multiple_notebooks(self):
        cards = [_card(name="physics"), _card(name="cooking-recipes")]

        prompt = build_classification_prompt(_summary(), cards)

        assert "physics" in prompt
        assert "cooking-recipes" in prompt

    def test_includes_json_format_instruction(self):
        prompt = build_classification_prompt(_summary(), [_card()])

        assert "JSON" in prompt
        assert "action" in prompt
        assert "notebook" in prompt

    def test_instructs_not_to_invent_notebook_names(self):
        prompt = build_classification_prompt(_summary(), [_card()])

        assert "一覧に無い" in prompt

    def test_instructs_assign_only_when_clearly_matching_description(self):
        """幻覚 assign 抑止: 実機ログで観測された『notebook 一般に適切』という
        捏造理由による誤 assign を防ぐため、明確な合致のみ assign を許す指示。"""
        prompt = build_classification_prompt(_summary(), [_card()])

        assert "明確に合致" in prompt

    def test_instructs_new_when_match_is_uncertain_or_mismatched(self):
        prompt = build_classification_prompt(_summary(), [_card()])

        assert "迷う場合" in prompt

    def test_instructs_description_is_mandatory_for_new_action(self):
        """実機ログで観測された『新規 notebook の description が空』問題への対処。
        description が空だと司書ルーティングが機能しなくなるため必須化を明示する。"""
        prompt = build_classification_prompt(_summary(), [_card()])

        assert "description を必ず入れて" in prompt

    def test_instructs_naming_at_category_granularity_not_individual_item(self):
        """実機ログで観測された『カレーレシピという個別資料名の粒度で notebook が
        作られ、別料理(カルボナーラ)がそこへ誤 assign される』問題への対処。
        name/description は個別資料の粒度でなく主題カテゴリの粒度で付けるよう指示する。"""
        prompt = build_classification_prompt(_summary(), [_card()])

        assert "カテゴリの粒度" in prompt

    def test_instructs_description_states_notebook_subject_not_item_summary(self):
        """description が『この資料は…』という個別資料の要約文になってしまうと、
        主題カテゴリで束ねる棚としての説明になっておらず、後続ルーティング品質が
        下がる。notebook の主題を説明する様式にするよう明示的に指示する。"""
        prompt = build_classification_prompt(_summary(), [_card()])

        assert "個別資料の要約文にしない" in prompt

    def test_is_deterministic_for_same_input(self):
        summary = _summary()
        cards = [_card()]

        first = build_classification_prompt(summary, cards)
        second = build_classification_prompt(summary, cards)

        assert first == second


class TestClassifySchema:
    def test_declares_required_fields(self):
        assert CLASSIFY_SCHEMA["type"] == "object"
        assert CLASSIFY_SCHEMA["required"] == ["action", "notebook", "reason"]

    def test_description_is_not_required(self):
        assert "description" not in CLASSIFY_SCHEMA["required"]
        assert "description" in CLASSIFY_SCHEMA["properties"]

    def test_action_is_restricted_to_assign_or_new(self):
        assert CLASSIFY_SCHEMA["properties"]["action"]["enum"] == ["assign", "new"]

    def test_forbids_additional_properties(self):
        assert CLASSIFY_SCHEMA["additionalProperties"] is False


class TestParseClassification:
    def test_parses_plain_assign_json(self):
        text = '{"action": "assign", "notebook": "physics", "reason": "主題が一致"}'

        decision = parse_classification(text)

        assert decision.parse_ok is True
        assert decision.action == "assign"
        assert decision.notebook == "physics"
        assert decision.reason == "主題が一致"
        assert decision.description is None

    def test_parses_new_json_with_description(self):
        text = (
            '{"action": "new", "notebook": "cooking-recipes", '
            '"description": "料理レシピ集", "reason": "既存に合致なし"}'
        )

        decision = parse_classification(text)

        assert decision.parse_ok is True
        assert decision.action == "new"
        assert decision.notebook == "cooking-recipes"
        assert decision.description == "料理レシピ集"

    def test_parses_json_fenced_in_markdown_code_block(self):
        text = (
            "```json\n"
            '{"action": "assign", "notebook": "physics", "reason": "主題が一致"}\n'
            "```"
        )

        decision = parse_classification(text)

        assert decision.parse_ok is True
        assert decision.notebook == "physics"

    def test_parses_json_with_leading_and_trailing_text(self):
        text = (
            "承知しました。\n"
            '{"action": "assign", "notebook": "physics", "reason": "主題が一致"}\n'
            "以上です。"
        )

        decision = parse_classification(text)

        assert decision.parse_ok is True

    def test_degrades_gracefully_on_broken_json(self):
        text = "これはJSONではない壊れたテキスト {action: 未閉じ"

        decision = parse_classification(text)

        assert decision.parse_ok is False

    def test_missing_action_field_is_parse_failure(self):
        text = '{"notebook": "physics", "reason": "主題が一致"}'

        decision = parse_classification(text)

        assert decision.parse_ok is False

    def test_missing_notebook_field_is_parse_failure(self):
        text = '{"action": "assign", "reason": "主題が一致"}'

        decision = parse_classification(text)

        assert decision.parse_ok is False

    def test_empty_notebook_field_is_parse_failure(self):
        text = '{"action": "assign", "notebook": "", "reason": "主題が一致"}'

        decision = parse_classification(text)

        assert decision.parse_ok is False

    def test_missing_reason_field_is_parse_failure(self):
        text = '{"action": "assign", "notebook": "physics"}'

        decision = parse_classification(text)

        assert decision.parse_ok is False

    def test_action_outside_enum_is_parse_failure(self):
        text = '{"action": "delete", "notebook": "physics", "reason": "主題が一致"}'

        decision = parse_classification(text)

        assert decision.parse_ok is False

    def test_non_dict_payload_is_parse_failure(self):
        text = '["assign", "physics"]'

        decision = parse_classification(text)

        assert decision.parse_ok is False

    def test_non_string_description_is_treated_as_absent(self):
        text = (
            '{"action": "new", "notebook": "cooking-recipes", '
            '"description": 123, "reason": "既存に合致なし"}'
        )

        decision = parse_classification(text)

        assert decision.parse_ok is True
        assert decision.description is None

    def test_is_deterministic_for_same_input(self):
        text = '{"action": "assign", "notebook": "physics", "reason": "主題が一致"}'

        first = parse_classification(text)
        second = parse_classification(text)

        assert first == second


class TestClassifyStepAssignToExistingNotebook:
    def test_assigns_to_existing_notebook_without_creating_new_one(self):
        decision = _decision(action="assign", notebook="physics", reason="主題が一致")
        catalog = [_card(name="physics")]

        result = classify_step(decision, _summary(), catalog, backend="ollama")

        assert result.assignment.notebook == "physics"
        assert result.assignment.new_notebook is False
        assert result.assignment.origin == "/abs/dir/note.md"
        assert result.assignment.summary == "ニュートン力学の講義ノート"
        assert result.assignment.reason == "主題が一致"
        assert result.new_notebook is None


class TestClassifyStepHallucinatedAssignReinterpretedAsNew:
    def test_assign_to_unknown_notebook_is_reinterpreted_as_new(self):
        decision = _decision(action="assign", notebook="does-not-exist", reason="関連あり")
        catalog = [_card(name="physics")]

        result = classify_step(decision, _summary(), catalog, backend="ollama")

        assert result.assignment.new_notebook is True
        assert result.assignment.notebook == "does-not-exist"
        assert result.new_notebook is not None
        assert result.new_notebook.name == "does-not-exist"
        assert result.new_notebook.backend == "ollama"

    def test_hallucinated_name_is_still_normalized(self):
        decision = _decision(action="assign", notebook="Does Not Exist!!", reason="関連あり")
        catalog = [_card(name="physics")]

        result = classify_step(decision, _summary(), catalog, backend="ollama")

        assert result.new_notebook.name == "does-not-exist"

    def test_hallucinated_assign_description_falls_back_to_file_summary(self):
        """幻覚 assign→new 再解釈経路でも description 空実害の対処が効くことを固定
        （実機ログ: 幻覚 assign の decision には description が乗らないため、この
        経路で作成される notebook の description が空のまま残ると司書ルーティングが
        機能しなくなる）。"""
        decision = _decision(action="assign", notebook="does-not-exist", reason="関連あり")
        catalog = [_card(name="physics")]
        summary = _summary(summary="カレーの作り方に関する資料")

        result = classify_step(decision, summary, catalog, backend="ollama")

        assert result.new_notebook.description == "カレーの作り方に関する資料"


class TestClassifyStepNewNotebook:
    def test_new_action_creates_notebook_spec(self):
        decision = _decision(
            action="new",
            notebook="cooking-recipes",
            description="料理レシピ集",
            reason="既存に合致なし",
        )
        catalog = [_card(name="physics")]

        result = classify_step(decision, _summary(), catalog, backend="ollama")

        assert result.assignment.notebook == "cooking-recipes"
        assert result.assignment.new_notebook is True
        assert result.new_notebook.name == "cooking-recipes"
        assert result.new_notebook.description == "料理レシピ集"
        assert result.new_notebook.backend == "ollama"

    def test_description_propagates_from_decision_to_created_spec(self):
        """description の created spec への伝播（設計書 §13.9 網羅対象）。"""
        decision = _decision(
            action="new", notebook="cooking-recipes", description="料理レシピ集", reason="r"
        )

        result = classify_step(decision, _summary(), [], backend="ollama")

        assert result.new_notebook.description == "料理レシピ集"

    def test_missing_description_falls_back_to_file_summary(self):
        """実機ログで観測された『新規 notebook の description が空』問題への対処。
        description が空だと司書ルーティングの唯一の判断材料が失われるため、
        FileSummary.summary を代替 description として採用する（劣化許容フォールバック）。"""
        decision = _decision(
            action="new", notebook="cooking-recipes", description=None, reason="既存に合致なし"
        )
        summary = _summary(summary="カレーレシピの資料")

        result = classify_step(decision, summary, [], backend="ollama")

        assert result.new_notebook.description == "カレーレシピの資料"

    def test_whitespace_only_description_falls_back_to_file_summary(self):
        decision = _decision(
            action="new", notebook="cooking-recipes", description="   ", reason="既存に合致なし"
        )
        summary = _summary(summary="カレーレシピの資料")

        result = classify_step(decision, summary, [], backend="ollama")

        assert result.new_notebook.description == "カレーレシピの資料"

    def test_missing_description_and_blank_summary_falls_back_to_empty_string(self):
        """summary も空の場合はフォールバック先が無いため空文字列のまま劣化許容する。"""
        decision = _decision(
            action="new", notebook="cooking-recipes", description=None, reason="既存に合致なし"
        )
        summary = _summary(summary="")

        result = classify_step(decision, summary, [], backend="ollama")

        assert result.new_notebook.description == ""

    def test_invalid_name_is_normalized(self):
        decision = _decision(
            action="new", notebook="Cooking Recipes!!", description="料理", reason="r"
        )

        result = classify_step(decision, _summary(), [], backend="ollama")

        assert result.new_notebook.name == "cooking-recipes"

    def test_colliding_name_gets_numeric_suffix(self):
        decision = _decision(action="new", notebook="physics", description="別の物理", reason="r")
        catalog = [_card(name="physics")]

        result = classify_step(decision, _summary(), catalog, backend="ollama")

        assert result.new_notebook.name == "physics-2"
        assert result.assignment.notebook == "physics-2"

    def test_backend_is_taken_from_caller_not_decision(self):
        decision = _decision(action="new", notebook="cooking-recipes", description="d", reason="r")

        result = classify_step(decision, _summary(), [], backend="codex")

        assert result.new_notebook.backend == "codex"


class TestClassifyStepParseFailure:
    def test_parse_failure_is_reinterpreted_as_new_with_default_name(self):
        decision = ClassificationDecision(
            action="", notebook="", reason="", parse_ok=False, description=None
        )
        summary = _summary(summary="ニュートン力学の講義ノート")

        result = classify_step(decision, summary, [], backend="ollama")

        assert result.assignment.new_notebook is True
        assert result.new_notebook.name == "notebook"
        # parse 失敗経路でも description 空実害対処のフォールバックが効くことを固定。
        assert result.new_notebook.description == "ニュートン力学の講義ノート"

    def test_parse_failure_with_blank_summary_stays_empty_description(self):
        decision = ClassificationDecision(
            action="", notebook="", reason="", parse_ok=False, description=None
        )
        summary = _summary(summary="")

        result = classify_step(decision, summary, [], backend="ollama")

        assert result.new_notebook.description == ""

    def test_parse_failure_reason_does_not_leak_raw_garbage(self):
        decision = ClassificationDecision(
            action="???", notebook="!!!", reason="???", parse_ok=False, description=None
        )
        catalog = [_card(name="notebook")]

        result = classify_step(decision, _summary(), catalog, backend="ollama")

        # 既定名 "notebook" が既にカタログに存在する場合は連番で衝突回避する。
        assert result.new_notebook.name == "notebook-2"


class TestClassifyStepIncrementalCatalogGrowth:
    """working カタログ成長・this-run 重複の吸収（設計書 §13.5 決定的セーフティネット）。"""

    def test_second_file_assigns_to_notebook_created_by_first_file(self):
        first_decision = _decision(
            action="new", notebook="physics", description="物理学の教材集", reason="新設"
        )
        first_result = classify_step(first_decision, _summary(origin="/a.md"), [], backend="ollama")
        assert first_result.new_notebook is not None

        working_catalog = [
            NotebookCard(
                name=first_result.new_notebook.name,
                description=first_result.new_notebook.description,
                persona=None,
                doc_count=0,
            )
        ]
        second_decision = _decision(action="assign", notebook="physics", reason="同一主題")

        second_result = classify_step(
            second_decision, _summary(origin="/b.md"), working_catalog, backend="ollama"
        )

        assert second_result.assignment.notebook == "physics"
        assert second_result.assignment.new_notebook is False
        assert second_result.new_notebook is None

    def test_pathological_second_new_for_same_name_gets_numeric_suffix(self):
        first_decision = _decision(
            action="new", notebook="physics", description="物理学の教材集", reason="新設"
        )
        first_result = classify_step(first_decision, _summary(origin="/a.md"), [], backend="ollama")

        working_catalog = [
            NotebookCard(
                name=first_result.new_notebook.name,
                description=first_result.new_notebook.description,
                persona=None,
                doc_count=0,
            )
        ]
        second_decision = _decision(
            action="new", notebook="physics", description="別カテゴリ", reason="敢えて新設"
        )

        second_result = classify_step(
            second_decision, _summary(origin="/b.md"), working_catalog, backend="ollama"
        )

        assert second_result.new_notebook.name == "physics-2"


class TestStepResultShape:
    def test_step_result_holds_assignment_and_optional_new_notebook(self):
        decision = _decision(action="assign", notebook="physics", reason="主題が一致")
        catalog = [_card(name="physics")]

        result = classify_step(decision, _summary(), catalog, backend="ollama")

        assert isinstance(result, StepResult)
        assert result.assignment is not None


class TestClassifyStepIsDeterministic:
    def test_same_input_yields_same_output(self):
        decision = _decision(action="new", notebook="Cooking Recipes!!", description="d", reason="r")
        catalog = [_card(name="physics")]

        first = classify_step(decision, _summary(), catalog, backend="ollama")
        second = classify_step(decision, _summary(), catalog, backend="ollama")

        assert first == second
