"""Behavior regressions: natural-language core steps, grounded audit, cumulative gate.

External generation/Judge calls are scripted; these tests do not measure model accuracy.
"""
import json
from pathlib import Path
import unittest
from examguard.config import Settings
from examguard.exam_policy import ExamPolicy
from examguard.leakage_guard import LLMLeakageJudge, LayeredOutputGuard, RuleLeakageScreener
from examguard.pipeline import ExamGuardTutor
from examguard.schemas import AuditResult, Classification, Intent, LLMResult, PolicyDecision, TutorRequest
from examguard.session_guard import SessionGuard, detect_disclosures

LEAK = json.loads((Path(__file__).parent/'fixtures/knapsack_delivered_leak.json').read_text())
STATE = '用表格记录考虑前 i 件物品、容量为 j 时的最大价值。'
TRANSITION = '装入时取该物品的价值加上前一件物品在剩余容量下的最大价值；不装入则保留前一件的值，两者取较大值。'
INIT = '没有物品或容量为零时，将表格中的值初始化为 0。'
ORDER = '外层逐个遍历物品，内层按容量从小到大计算各个状态。'
P = PolicyDecision(Intent.HINT, 2, 'test')
C = Classification(Intent.HINT, 'test', '')

class Backend:
    def __init__(self, values): self.values=iter(values)
    def generate(self, messages): return LLMResult(next(self.values), 'scripted', 1, 1)

def payload(disclosures=None, **overrides):
    obj=dict(leaked=False, categories=[], confidence=0.95, reason='概念', disclosures=disclosures or [])
    obj.update(overrides)
    return json.dumps(obj, ensure_ascii=False)

class NaturalLanguageGateTests(unittest.TestCase):
    def test_ledger_alias_still_binds_subproblem_values_and_both_branches(self):
        text=json.loads((Path(__file__).parent/'fixtures/knapsack_ledger_alias.json').read_text())['answer']
        for level in [1,2]:
            with self.subTest(level=level):
                a=RuleLeakageScreener().review(text,PolicyDecision(Intent.KNOWLEDGE,level,'test'),True)
                self.assertFalse(a.approved)

    def test_hint_cannot_deliver_both_branch_values_and_their_optimization(self):
        text=json.loads((Path(__file__).parent/'fixtures/knapsack_hint_branches.json').read_text())['answer']
        a=RuleLeakageScreener().review(text,P,True)
        self.assertFalse(a.approved)
        self.assertIn('transition',a.disclosures)

    def test_specific_problem_state_binding_at_concept_level_is_not_general_concept(self):
        text=json.loads((Path(__file__).parent/'fixtures/knapsack_state_prose.json').read_text())['answer']
        a=RuleLeakageScreener().review(text,PolicyDecision(Intent.KNOWLEDGE,1,'test'),True)
        self.assertFalse(a.approved)
        self.assertIn('state_definition',a.disclosures)

    def test_supplied_semicolon_code_remains_a_legal_complexity_quote(self):
        code='dp[i] = dp[i-1]; x = 1'
        text='你已有的 `'+code+'` 每次只做常数次操作，因此这两条赋值的时间复杂度是 O(1)。'
        self.assertTrue(RuleLeakageScreener().review(text,PolicyDecision(Intent.KNOWLEDGE,1,'test'),True,existing_code=code).approved)

    def test_preserved_real_delivered_leak_is_blocked_for_each_intent(self):
        for intent, level in [(Intent.KNOWLEDGE,1),(Intent.SOLUTION,1),(Intent.HINT,2),(Intent.DEBUG,3)]:
            with self.subTest(intent=intent):
                a=RuleLeakageScreener().review(LEAK['answer'],PolicyDecision(intent,level,'test'),True,LEAK['question'],'x=1' if level==3 else '')
                self.assertFalse(a.approved)
                self.assertNotIn('当前总价值', a.output)

    def test_formula_free_recurrence_and_refusal_prefix_are_blocked(self):
        for text in [TRANSITION, '不能给完整代码，但'+TRANSITION, '不提供公式。'+TRANSITION]:
            with self.subTest(text=text):
                self.assertFalse(RuleLeakageScreener().review(text,P,True).approved)

    def test_mixed_original_code_quote_does_not_erase_new_recurrence(self):
        text='你已有 `x = 1`，'+TRANSITION
        self.assertFalse(RuleLeakageScreener().review(text,P,True,existing_code='x = 1').approved)

    def test_full_structure_without_formula_or_numbered_steps_is_blocked(self):
        self.assertFalse(RuleLeakageScreener().review(STATE+INIT+ORDER,P,True).approved)

    def test_natural_disclosures_are_detected_independently_of_intent(self):
        got=set(detect_disclosures(STATE+TRANSITION+INIT+ORDER,Intent.KNOWLEDGE,1))
        self.assertTrue({'state_definition','transition','initialization','traversal'} <= got,got)

    def test_concepts_complexity_and_existing_local_fix_are_retained(self):
        for text,level,code in [
            ('动态规划的状态是对子问题信息的抽象；初始化、转移和遍历是不同概念。',1,''),
            ('最优子结构意味着整体最优解包含子问题的最优解，不保证可以贪心。',1,''),
            ('因为有 nW 个状态且各需常数时间，总时间为 O(nW)。',1,''),
            ('第 2 行闭区间右端点越界，改为 `r = len(a) - 1`。',3,'l, r = 0, len(a)'),
            ('请先自己定义状态，再思考边界和遍历顺序。',2,''),
            ('W 表示容量限制，v 表示物品价值。',1,''),
            ('W 表示容量限制，v 表示物品的最大价值。',1,''),
            ('W 表示背包容量上限，v 表示物品价值。',1,''),
            ('0/1背包的每件物品最多选一次，所有选中物品的总重量不能超过容量。',2,''),
            ('最优子结构是指当前最优值来自子问题的最优解。',1,''),
            ('请思考当前状态应当是什么，而不是直接套模板。',1,''),
            ('不能告诉你当前状态取什么值，请先写出自己的推导。',1,''),
        ]:
            with self.subTest(text=text):
                a=RuleLeakageScreener().review(text,PolicyDecision(Intent.DEBUG if code else Intent.KNOWLEDGE,level,'test'),True,existing_code=code)
                self.assertTrue(a.approved,a.reason)

class GroundedJudgeTests(unittest.TestCase):
    def test_stable_reference_resolves_to_exact_candidate_evidence(self):
        text='甲格保存最优值。'
        a=self.review(text,payload([dict(element='state_definition',ref='C1')]))
        self.assertIsNotNone(a)
        self.assertTrue(a.approved)
        self.assertEqual(a.evidence[-1]['quote'],text)
        self.assertEqual(a.evidence[-1]['source'],'candidate')

    def test_nonexistent_reference_is_unknown(self):
        self.assertIsNone(self.review('一般概念。',payload([dict(element='transition',ref='C999')])) )

    def test_judge_mixed_quote_keeps_new_disclosure(self):
        text='你已有 `x = 1`，'+TRANSITION
        j=LLMLeakageJudge(Backend([payload([dict(element='transition',quote=text,source='candidate')])]),Settings())
        a=j.review(text,P,True,existing_code='x = 1')
        self.assertIsNotNone(a);self.assertFalse(a.approved)

    def review(self,text,value,history=None,level=2):
        j=LLMLeakageJudge(Backend([value]),Settings())
        return j.review(text,PolicyDecision(Intent.HINT,level,'test'),True,history=history)

    def test_positive_boolean_cannot_override_disclosed_transition(self):
        text='这一格的结果等于上方那格与左上方那格中的较大者。'
        a=self.review(text,payload([dict(element='transition',quote=text,source='candidate')]))
        self.assertIsNotNone(a)
        self.assertFalse(a.approved)

    def test_missing_evidence_schema_is_unknown(self):
        self.assertIsNone(self.review('一般概念。','{"leaked":false,"categories":[],"confidence":0.95}'))

    def test_contradictory_pass_with_leak_categories_is_unknown(self):
        self.assertIsNone(self.review('一般概念。',payload(categories=['full_solution'])))

    def test_fabricated_quote_or_invalid_element_is_unknown(self):
        for item in [dict(element='transition',quote='原文不存在',source='candidate'),dict(element='made_up',quote='一般概念。',source='candidate'),dict(element='state_definition',quote='一般概念。',source='tool')]:
            with self.subTest(item=item): self.assertIsNone(self.review('一般概念。',payload([item])))

    def test_grounded_history_and_candidate_are_combined(self):
        old='甲格保存前缀的最优值。'; new='乙格是把甲格减少一个单位后与丙格取较大值。'
        a=self.review(new,payload([dict(element='state_definition',quote=old,source='history'),dict(element='transition',quote=new,source='candidate')]),history=[old],level=3)
        self.assertIsNotNone(a);self.assertFalse(a.approved)

    def test_history_alone_does_not_block_repeated_general_concept(self):
        old=STATE
        a=self.review('复杂度衡量资源随规模的增长。',payload([dict(element='state_definition',quote=old,source='history')]),history=[old])
        self.assertTrue(a.approved)

class CumulativeSemanticTests(unittest.TestCase):
    def test_two_branch_values_and_optimization_cannot_be_split_across_turns(self):
        g=SessionGuard();req=TutorRequest('背包问题给方向',question_id='q');state,_=g.before(req,C,P)
        first='情况 A：放入背包会占用容量，并累加该物品的价值。'
        second='情况 B：不放入背包时，容量不减少，只保留当前已有的价值。比较上述两种情况哪一边收益更大。'
        a=RuleLeakageScreener().review(first,P,True)
        self.assertTrue(a.approved)
        g.record(state,req,C,P,a)
        b=RuleLeakageScreener().review(second,P,True)
        self.assertTrue(b.approved)
        self.assertFalse(g.preview(state,req,C,P,b).approved)

    def test_followup_uses_last_active_question_even_after_snapshot_key_reorder(self):
        g=SessionGuard();ra=TutorRequest('背包题如何定义子问题，给方向')
        a,_=g.before(ra,C,P);g.record(a,ra,C,P,AuditResult(True,STATE,'ok'))
        rb=TutorRequest('最短路与路径长度概念')
        b,_=g.before(rb,C,P);g.record(b,rb,C,P,AuditResult(True,'路径长度是边权之和。','ok'))
        again,_=g.before(ra,C,P);self.assertIs(again,a)
        g.record(again,ra,C,P,AuditResult(True,'先检查你的定义是否清楚。','ok'))
        snapshot=json.loads(json.dumps(g.snapshot(),sort_keys=True))
        restored=SessionGuard();restored._states={k:type(a).from_dict(v) for k,v in snapshot.items()}
        follow=TutorRequest('接着解释刚才这道题的边界和循环顺序')
        state,_=restored.before(follow,C,P)
        self.assertIn('state_definition',state.disclosed_elements)
        self.assertFalse(restored.preview(state,follow,C,P,AuditResult(True,INIT+ORDER,'ok')).approved)

    def test_judge_only_alias_evidence_is_committed_and_restored(self):
        text='甲格保存最优值，具体含义如上。'
        audit=LLMLeakageJudge(Backend([payload([dict(element='state_definition',quote=text,source='candidate')])]),Settings()).review(text,P,True)
        g=SessionGuard();req=TutorRequest('题目',question_id='q');state,_=g.before(req,C,P)
        g.record(state,req,C,P,audit)
        saved=type(state).from_dict(state.to_dict())
        self.assertIn('state_definition',saved.disclosed_elements)
        self.assertFalse(g.preview(saved,req,C,P,AuditResult(True,INIT+ORDER,'ok')).approved)

    def test_refusal_prefixed_history_cannot_overflow_the_judge_context(self):
        g=SessionGuard();req=TutorRequest('题目',question_id='q');state,_=g.before(req,C,P)
        state.disclosure_texts=['先前概念。'*3900]
        text='不提供程序，但可以解释概念。'*150
        self.assertFalse(g.preview(state,req,C,P,AuditResult(True,text,'ok')).approved)

    def test_definition_initialization_traversal_cannot_be_assembled_after_restore(self):
        g=SessionGuard(); req=TutorRequest('新的背包题给方向',question_id='q')
        state,_=g.before(req,C,P)
        g.record(state,req,C,P,AuditResult(True,STATE,'ok'))
        restored=SessionGuard();restored._states={k:type(state).from_dict(v) for k,v in g.snapshot().items()}
        state,_=restored.before(req,C,P)
        a=restored.preview(state,req,C,P,AuditResult(True,INIT+ORDER,'ok'))
        self.assertFalse(a.approved)
        restored.record(state,req,C,P,a)
        self.assertNotIn('initialization',state.disclosed_elements)

    def test_explicit_followup_links_even_if_labeled_knowledge(self):
        g=SessionGuard();r=TutorRequest('我做新的背包题，只讲状态')
        state,_=g.before(r,C,P);g.record(state,r,C,P,AuditResult(True,STATE,'ok'))
        req=TutorRequest('接着解释刚才这道题的边界和循环顺序')
        k=Classification(Intent.KNOWLEDGE,'test','')
        same,_=g.before(req,k,PolicyDecision(Intent.KNOWLEDGE,1,'test'))
        self.assertIs(same,state)
        self.assertFalse(g.preview(same,req,k,P,AuditResult(True,INIT+ORDER,'ok')).approved)

    def test_pipeline_never_sends_rejected_prose_and_regenerates_useful_hint(self):
        tutor=ExamGuardTutor(Backend([TRANSITION,'先检查你的子问题是否包含了约束所需的信息。']),Settings(output_guard='rules'),policy_config=ExamPolicy.from_dict({'max_regenerations':1}))
        r=tutor.respond(TutorRequest('给背包题一点方向'),session_guard=SessionGuard())
        self.assertTrue(r.audit.approved)
        self.assertNotIn('两者取较大值',r.answer)
        self.assertEqual(r.regenerations,1)
        self.assertNotIn('transition',r.session_disclosures)
