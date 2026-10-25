const test = require('node:test');
const assert = require('node:assert/strict');
const ui = require('../presentation.js');

test('policy denial explains missing authorization and observed zero executions', () => {
  const status = {phase: 'DENIED', reason_code: 'UNRESOLVED_REQUIREMENTS', executed: 0};
  const cards = ui.summaries(ui.fresh(), status, null);
  assert.equal(cards.length, 1);
  assert.ok(cards[0].tags.includes('授权条件不足'));
  assert.ok(cards[0].tags.includes('任务策略'));
  assert.ok(cards[0].tags.includes('工具执行 0 次'));
});

test('result quarantine never claims the tool was prevented from executing', () => {
  const state = ui.consume(ui.fresh(), {kind: 'review_finished', phase: 'RESULT', decision: 'BLOCK', reason_code: 'UNTRUSTED_INSTRUCTION', args: 'UNRELEASED_TOOL_TEXT'});
  const card = ui.summaries(state, {}, null)[0];
  assert.ok(card.tags.includes('资料指令注入'));
  assert.ok(card.tags.includes('隔离工具结果'));
  assert.match(card.how, /后续模型上下文/);
  assert.doesNotMatch(card.how, /执行前|撤销|回滚/);
  assert.doesNotMatch(JSON.stringify(card), /UNRELEASED_TOOL_TEXT/);
});

test('block explanation survives generic receipts and safe context recovery', () => {
  let state = ui.consume(ui.fresh(), {kind: 'review_finished', phase: 'ACTION', decision: 'BLOCK', reason_code: 'DESTINATION_OUT_OF_SCOPE'});
  state = ui.consume(state, {kind: 'receipt', status: 'BLOCKED', reason_code: 'GUARD_DECISION'});
  state = ui.consume(state, {kind: 'status', status: {phase: 'RECOVERED', recoveries: 1}});
  const card = ui.summaries(state, {phase: 'COMPLETE'}, {outcome: 'guard_block'})[0];
  assert.equal(card.code, 'DESTINATION_OUT_OF_SCOPE');
  assert.ok(card.tags.includes('执行前阻断'));
  assert.ok(card.tags.includes('已恢复安全上下文'));
});

test('review failure and model refusal are not credited as guard detection', () => {
  const state = ui.consume(ui.fresh(), {kind: 'review_finished', phase: 'RESULT', decision: 'FAILED', reason_code: 'INVALID_REFERENCE'});
  assert.equal(ui.summaries(state, {}, null)[0].kind, 'error');
  assert.doesNotMatch(JSON.stringify(ui.summaries(state, {}, null)), /AgentGuard 已拦截/);
  const refusal = ui.summaries(ui.fresh(), {}, {outcome: 'model_refusal'}, 'off');
  assert.equal(refusal[0].title, '模型自行拒绝');
});

test('historical records with no phase do not invent an interception stage', () => {
  const cards = ui.summaries(ui.fresh(), {}, {outcome: 'guard_block', reason_codes: ['GUARD_DECISION']});
  assert.ok(cards[0].tags.includes('防护门禁'));
  assert.ok(!cards[0].tags.includes('结果审查'));
  assert.ok(!cards[0].tags.includes('工具执行 0 次'));
});

test('redaction request and missing evidence are never promoted to completed protection', () => {
  const state = ui.consume(ui.fresh(), {kind: 'review_finished', phase: 'FINAL', decision: 'REDACT', reason_code: 'SENSITIVE_DATA'});
  assert.equal(ui.summaries(state, {}, null)[0].title, '审查要求脱敏');
  const applied = ui.summaries(state, {redacted: 2}, null)[0];
  assert.equal(applied.title, '已记录脱敏处理');
  assert.ok(applied.tags.includes('脱敏 2 次'));
  assert.match(ui.isolation({}).title, /待生成/);
  assert.match(JSON.stringify(ui.evidence({})), /暂无记录/);
  assert.doesNotMatch(JSON.stringify(ui.evidence({})), /脱敏.*已完成/);
});

test('observed harm remains visible even when an interception was recorded', () => {
  const state = ui.consume(ui.fresh(), {kind: 'review_finished', phase: 'FINAL', decision: 'BLOCK', reason_code: 'DATA_USE_DENIED'});
  assert.ok(ui.summaries(state, {}, {attack_success: true}).some(card => card.title === '观察到危险效果'));
});

test('all seven showcases distinguish native steps and recorded isolation facts', () => {
  assert.equal(new Set(ui.demos.map(d => d.id)).size, 7);
  assert.equal(ui.demos.find(d => d.id === 'boundary').caseId, 'g-permission');
  assert.match(ui.demos.find(d => d.id === 'native').watch, /网页不模拟开关/);
  const isolation = ui.isolation({isolation: {ready: true, network_denied: true, host_loopback_denied: false}});
  assert.deepEqual(isolation.tags, ['外网直连受限']);
});
