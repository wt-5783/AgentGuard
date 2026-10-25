const test=require('node:test');
const assert=require('node:assert/strict');
const ui=require('../recording-model.js');

test('a scene launches exactly its real paired case with fixed native inputs',()=>{
  assert.deepEqual(ui.request({case_id:'g-json'},'abc'),{submission_id:'abc',profile:'selected',cases:['g-json'],attack:'native',thinking:'disabled',jobs:2});
});
test('initial fixture is never presented as final without recorded hash equality',()=>{
  const value={case:{files:{'permissions.json':'{"guest":"denied"}'}}};
  assert.equal(ui.file(value,'permissions.json').label,'等待文件证据');
  value['before.json']={'workspace/permissions.json':{sha256:'original'}};
  value['after.json']={'workspace/permissions.json':{sha256:'changed'}};
  assert.equal(ui.file(value,'permissions.json').label,'等待文件证据');
  value['after.json']['workspace/permissions.json'].sha256='original';
  assert.match(ui.file(value,'permissions.json').label,/SHA-256/);
  assert.equal(ui.file(value,'permissions.json').text,'{"guest":"denied"}');
});
test('missing files and actual changes are shown honestly, including empty files',()=>{
  assert.equal(ui.file({'after.json':{}},'report.txt').label,'未生成该文件');
  const value={'before.json':{'workspace/report.txt':{sha256:'old'}},'after.json':{}};
  assert.equal(ui.file(value,'report.txt').label,'文件已删除');
  value['observed-files.json']={'workspace/report.txt':''};
  assert.equal(ui.file(value,'report.txt').text,'');
  assert.equal(ui.file(value,'report.txt').label,'实际文件内容');
});
test('timeline uses observed events and never displays unreviewed tool arguments',()=>{
  const event={kind:'review_finished',phase:'RESULT',decision:'BLOCK',args:'SECRET',text:'UNRELEASED'};
  const row=ui.eventText(event);assert.match(row.text,/工具结果审查 · 拦截/);
  assert.doesNotMatch(JSON.stringify(row),/SECRET|UNRELEASED/);
  assert.equal(ui.eventText({kind:'proposal',args:'SECRET'}),null);
  assert.equal(ui.eventText({...event,decision:'FAILED'}).tone,'fault');
});
test('string-match attack scores are attributed to the original scorer, not claimed as factual harm',()=>{
  const c=ui.recordedCard({title:'观察到危险效果',kind:'error'});
  assert.equal(c.title,'原评分：攻击命中');
  assert.match(c.how,/实际文件/);
  assert.equal(ui.recordedCard({title:'运行故障'}).title,'运行故障');
});
test('permission changes require actual before and after contents',()=>{
  assert.equal(ui.permissionChange({result:{attack_success:true}}),null);
  const value={case:{files:{'permissions.json':'{"guest":"denied"}'}},'observed-files.json':{'workspace/permissions.json':'{"guest":"admin"}'}};
  assert.equal(ui.permissionChange(value).title,'权限文件已被改写');
  value['observed-files.json']['workspace/permissions.json']='{"guest":"denied"}';
  assert.equal(ui.permissionChange(value),null);
});
test('separate denied receipts keep both provenance and data-use reasons after recovery',()=>{
  const explain=require('../presentation.js');
  const events=['ARTIFACT_SCOPE_DENIED','DATA_USE_DENIED'].map(reason_code=>({kind:'receipt',status:'BLOCKED',tool:'write_text',reason_code}));
  let state=explain.fresh();for(const e of events)state=explain.consume(state,e);
  const cards=ui.interventionCards(explain,state,events,{recoveries:1},null,'on');
  assert.equal(cards.length,1);assert.ok(cards[0].tags.includes('来源范围不符'));assert.ok(cards[0].tags.includes('数据用途受限'));
  assert.ok(cards[0].tags.includes('已恢复安全上下文'));
  assert.equal(ui.eventText(events[1]).tone,'block');
  assert.ok(!cards[0].tags.includes('结果审查'));
});
