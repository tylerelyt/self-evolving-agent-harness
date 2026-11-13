// UI state and safe Markdown regressions. Run: node workshop/capstone/web/test_ui.cjs
const fs=require('fs'), path=require('path'), vm=require('vm'), assert=require('assert');
const html=fs.readFileSync(path.join(__dirname,'index.html'),'utf8');
const nodes=new Map();
function node(id){if(!nodes.has(id))nodes.set(id,{id,value:'',dataset:{},innerHTML:'',textContent:'',hidden:false,disabled:false,style:{},scrollHeight:0,scrollTop:0,clientHeight:400,open:false,classList:{toggle(){}},querySelectorAll(){return[]},setAttribute(){},addEventListener(){},focus(){},showModal(){this.open=true},close(){this.open=false},getBoundingClientRect(){return {left:0,right:400,top:0,bottom:900}}});return nodes.get(id)}
for(const match of html.matchAll(/id="([^"]+)"/g))node(match[1]);
const nav=[...html.matchAll(/data-page="([^"]+)"/g)].map(m=>({...node('nav-'+m[1]),dataset:{page:m[1]}}));
const pages=['overview','metrics','logs','changes','memory','learning'].map(node);
const calls=[], chats={};
let snapshotOverride=null;
let pendingRecovery=null;
const state={root:'/tmp/ui-test-workbench',time:1234,agent:{status:'offline',mode:'local-script-model',active_stage:0,stages:[1,2,3,4].map(number=>({number,title:'step '+number,implemented:false})),implementation:'/fixture/learner.py',code_sha256:'abc'},chat:{available:false,mode:'offline'},instances:[{id:'instance-a',services:[{name:'payments',health:'healthy',metrics:{requests:80,error_rate:0,p95_ms:25},history:[]}]}],incidents:[],business:{logs:[],changes:[],alerts:[{instance:'instance-a',service:'payments',incident_id:'lab-payments-0123456789ab',status:'open',emitted_at:1230}]},sessions:[],memory:{facts:[],recalls:[]},learning:{batches:[]},checks:[],events:[],conversations:[]};
const sandbox={console,document:{getElementById:node,querySelector(selector){return selector==='.workspace'?node('workspace'):null},querySelectorAll(selector){return selector==='.page'?pages:selector==='.nav'?nav:[]},addEventListener(){},title:''},localStorage:{getItem(){return null},setItem(){},removeItem(){}},navigator:{clipboard:{writeText:async()=>{}}},setTimeout(){},clearTimeout(){},setInterval(){},fetch:async(path,opts)=>{if(opts?.body){const body=JSON.parse(opts.body);calls.push(body);if(body.action==='chat_new'){const conversation={id:'conversation-'+(Object.keys(chats).length+1),title:'Investigation',status:'idle'};state.conversations.unshift(conversation);chats[conversation.id]={conversation,messages:[],threads:[],jobs:[]};return{ok:true,json:async()=>({conversation})};}if(body.action==='recovery_request'){
 const record=state.incidents.find(item=>item.source===body.source&&item.incident_id===body.incident_id);
 if(!record?.page)throw new Error('Missing paging evidence');
 pendingRecovery={status:'awaiting_approval',source:record.source,incident_id:record.incident_id,operation:'bound-operation',scope:'current-environment/payments',instance:record.instance,service:record.service,environment_id:'current-environment',action:'clear_lab_fault',report:record.investigation.report,session_id:'investigation-session',report_item_id:30,approval:null,execution:null};
 record.recovery=JSON.parse(JSON.stringify(pendingRecovery));
 return {ok:true,json:async()=>JSON.parse(JSON.stringify(pendingRecovery))};
}if(body.action==='recovery_approve'){
 const verified={...pendingRecovery,status:'verifying',approval:{actor:'local-workbench-user',source:'local_workbench_http',identity_verified:false,confirmed:true},execution:{status:'executed'}};
 // Keep the state response stale to reproduce an in-flight polling snapshot.
 return {ok:true,json:async()=>verified};
}if(body.action==='chat_send'){chats[body.conversation].messages.push({id:calls.length,role:'user',content:body.question});return{ok:true,json:async()=>({accepted:true})};}}if(path==='/api/state')return{ok:true,json:async()=>JSON.parse(JSON.stringify(snapshotOverride||state))};if(path.startsWith('/api/chat?'))return{ok:true,json:async()=>JSON.parse(JSON.stringify(chats[new URL('http://local'+path).searchParams.get('conversation')]))};throw new Error('Unexpected request '+path);}};
vm.createContext(sandbox);vm.runInContext(html.split('<script>')[1].split('</script>')[0],sandbox);
(async()=>{await new Promise(resolve=>setImmediate(resolve));

 assert.equal(node('workspace').textContent,'本地运行工作区');
 assert(!node('code-path').textContent.includes('/fixture/'),'implementation display must not reveal machine paths');
 assert.equal(vm.runInContext("implementationLabel('/Users/private/project/apps/oncall/workflow.py')",sandbox),'apps/oncall/workflow.py');
 assert.equal(vm.runInContext("implementationLabel('/private/tmp/custom-secret.py')",sandbox),'外部实现文件');
 assert.equal(state.root,'/tmp/ui-test-workbench','display labels must preserve backend path bindings');
 assert.equal(node('open-practice').textContent,'练习与运行','legacy/tutorial snapshots retain the coding guide');
 assert.equal(node('tutorial-controls').hidden,false);
 state.mode='system';state.agent.stages=[];await vm.runInContext('refresh()',sandbox);
 assert.equal(node('open-practice').textContent,'运行与管理');
 assert.equal(node('practice-title').textContent,'运行与管理');
 assert.equal(node('tutorial-controls').hidden,true,'maintained runtime must hide coding exercises');
 assert.equal(node('stages').innerHTML,'');
 assert.equal(node('check').disabled,true);
 assert(!node('capability-note').textContent.includes('应用代码'));
 const beforeCheck=calls.length;node('check').onclick();await node('copy-prompt').onclick();
 assert.equal(calls.length,beforeCheck,'system mode must not issue hidden teaching commands');
 state.mode='tutorial';state.agent.stages=[1,2,3,4].map(number=>({number,title:'step '+number,implemented:false}));await vm.runInContext('refresh()',sandbox);
 assert.equal(node('tutorial-controls').hidden,false,'explicit tutorial mode restores the guide');
 assert(node('chat-question').disabled,'offline must disable chat');
 assert(node('overview-content').innerHTML.includes('监控已发出告警'),'offline raw alerts must be visible');
 assert(!node('overview-content').innerHTML.includes('learner.py'),'source path must stay out of business content');
 state.agent.status='running';state.agent.active_stage=4;state.agent.stages.forEach(x=>x.implemented=true);state.chat.available=true;await vm.runInContext('refresh()',sandbox);
 vm.runInContext("selectedIncident=key(incidentRecords()[0]);",sandbox);
 await vm.runInContext("sendQuestion('调查这个告警')",sandbox);
 let first=calls.find(x=>x.action==='chat_send');assert(first.context.incident_id==='lab-payments-0123456789ab');assert(first.context.instance==='instance-a');assert(first.context.service==='payments');
 vm.runInContext("switchPage('logs')",sandbox);await vm.runInContext("sendQuestion('刚才的结论依据是什么？')",sandbox);
 const sends=calls.filter(x=>x.action==='chat_send');assert.equal(sends.length,2);assert.equal(sends[0].conversation,sends[1].conversation,'cross-page follow-up must retain conversation');assert.equal(sends[1].context.page,'logs');assert.equal(calls.filter(x=>x.action==='chat_new').length,1,'follow-up must not silently create another conversation');
 assert(node('chat-messages').innerHTML.includes('刚才的结论依据是什么？'),'persisted response messages should render');
 const escaped=vm.runInContext("messageHtml({role:'assistant',content:'<img src=x onerror=alert(1)>'})",sandbox);assert(!escaped.includes('<img'),'chat content must be escaped');
 state.business.alerts[0].status='recovered';state.incidents=[{source:'run/instance-a',instance:'instance-a',service:'payments',incident_id:'lab-payments-0123456789ab',status:'recovered',report:{analysis:'Actual report'},session_id:'native-session',report_item_id:10}];await vm.runInContext('refresh()',sandbox);assert.equal(vm.runInContext('incidents().length',sandbox),1,'raw alert and investigation must not double count');assert(node('overview-content').innerHTML.includes('Actual report'));assert(!node('overview-content').innerHTML.includes('等待 Agent 接手调查'));

 assert.equal(vm.runInContext('context().incident_id',sandbox),'lab-payments-0123456789ab','saving an investigation must retain the selected monitor incident');
 snapshotOverride=JSON.parse(JSON.stringify(state));
 const createdId=await vm.runInContext('newConversation()',sandbox);
 assert.equal(vm.runInContext('conversationId',sandbox),createdId,'an older polling snapshot must not select an old conversation after creation');
 snapshotOverride=null;await vm.runInContext('refresh()',sandbox);
 assert.equal(vm.runInContext('conversationId',sandbox),createdId);


 state.business.alerts[0].status='handed_off';
 Object.assign(state.incidents[0],{status:'handed_off',environment_id:'current-environment',page:{report:{recovery_plan:{action:'clear_lab_fault',approval_required:true}}},investigation:{session_id:'investigation-session',report_item_id:30,report:{analysis:'The scoped service has persistent errors.',recovery_plan:{action:'clear_lab_fault',approval_required:true,verification:'two_clean_windows'}}}});
 await vm.runInContext('refresh()',sandbox);
 await vm.runInContext('requestRecovery(incidentRecords()[0])',sandbox);
 assert.equal(calls.filter(call=>call.action==='recovery_request').length,1);
 assert.equal(calls.filter(call=>call.action==='recovery_approve').length,0,'requesting recovery must not approve or execute it');
 assert(node('recovery-dialog').open,'show a concrete request before approval');
 assert(node('recovery-content').innerHTML.includes('current-environment/payments'));
 assert(node('recovery-content').innerHTML.includes('lab-payments-0123456789ab'));
 assert(node('recovery-content').innerHTML.includes('本地演练授权'));
 assert(!node('approve-recovery').disabled,'a current paged incident may be explicitly approved');
 assert.equal(vm.runInContext('canRequestRecovery({...incidentRecords()[0],page:null})',sandbox),false,'unpaged incidents must not be offered recovery');
 assert.equal(vm.runInContext('canApproveRecovery({...recoveryRequest,environment_id:"another-environment"})',sandbox),false,'a changed scope must not be approved');
 await vm.runInContext('approveRecovery()',sandbox);
 const approved=calls.find(call=>call.action==='recovery_approve');
 assert.deepEqual(approved,{action:'recovery_approve',source:'run/instance-a',incident_id:'lab-payments-0123456789ab',operation:'bound-operation',scope:'current-environment/payments',environment_id:'current-environment',confirmed:true},'explicit approval must retain the exact requested scope');
 assert(node('recovery-content').innerHTML.includes('恢复验证中'));
 assert.equal(vm.runInContext('recoveryRequest.status',sandbox),'verifying','a stale poll must not revert an executed operation to awaiting approval');
 assert(node('approve-recovery').hidden,'a received execution must not be immediately re-approved');
 await vm.runInContext('requestRecovery(incidentRecords()[0])',sandbox);
 assert.equal(vm.runInContext('recoveryRequest.status',sandbox),'verifying','reopening a stale card must not restore an already executed approval');
 assert(node('approve-recovery').hidden,'reopening recovery evidence must not offer duplicate approval');
 assert(!calls.some(call=>call.action==='fault'&&call.mode==='off'),'the UI must not clear a fault through the injection endpoint');
 state.incidents[0].status='recovered';state.business.alerts[0].status='recovered';
 await vm.runInContext('refresh()',sandbox);
 assert.equal(vm.runInContext('recoveryRequest.status',sandbox),'verifying','a patrol recovered flag must not override the controlled recovery ledger');
 state.incidents[0].recovery={...pendingRecovery,status:'verifying',runtime_state:'VERIFYING'};
 await vm.runInContext('refresh()',sandbox);
 assert.equal(vm.runInContext('recoveryRequest.status',sandbox),'verifying','approval remains verifying until the runtime has closed');
 state.incidents[0].recovery={...pendingRecovery,status:'recovered',runtime_state:'CLOSED'};
 await vm.runInContext('refresh()',sandbox);
 assert.equal(vm.runInContext('recoveryRequest.status',sandbox),'recovered','only the authoritative controlled-recovery snapshot closes the approved operation');
 assert.equal(vm.runInContext('evaluationBadge({valid_trace:false,passed:false})',sandbox).includes('证据不足'),true);
 assert.equal(vm.runInContext('evaluationBadge({valid_trace:false,passed:false})',sandbox).includes('失败'),false,'missing evidence must not be shown as a business failure');
 assert.equal(vm.runInContext('evaluationBadge({valid_trace:true,passed:false})',sandbox).includes('失败'),true);
 assert.equal(vm.runInContext('evaluationBadge({valid_trace:true,passed:true})',sandbox).includes('通过'),true);


 const failedModel={id:'model-error',status:'failed',output:'SDK retry\nInsufficient Balance\n/fixture/internal-dump.json\n'+('debug line\n'.repeat(500))};
 const failedHtml=vm.runInContext('jobFailureHtml('+JSON.stringify(failedModel)+')',sandbox);
 assert(failedHtml.includes('模型服务余额不足，本次调查未完成。'));
 assert(failedHtml.includes('<details')&&!failedHtml.includes('<details open'),'lengthy SDK diagnostics must be collapsed by default');
 assert(failedHtml.includes('运行记录')&&failedHtml.includes('/fixture/internal-dump.json'),'the original failure evidence remains available');
 assert(!failedHtml.split('<details')[0].includes('SDK retry'),'the default error must not dump retries into the conversation');
 assert(vm.runInContext('jobFailureSummary({status:"interrupted",output:"long trace"})',sandbox).includes('已中断'));

 function markdown(value){return vm.runInContext('textHtml('+JSON.stringify(value)+')',sandbox,{timeout:1000});}
 const report=markdown('## Investigation\n\nObserved **HTTP errors**.\n\n| Service | Current |\n| --- | ---: |\n| payments | 503 |\n| orders | 200 |\n\n### Next steps\n\n1. Verify the evidence\n2. Page the engineer\n   - Include the time window\n   - Cite the actual report');
 assert(report.includes('<h2>Investigation</h2>'));
 assert(report.includes('<strong>HTTP errors</strong>'));
 assert(report.includes('<div class="markdown-table"><table>'));
 assert(report.includes('<thead>')&&report.includes('<tbody>')&&report.includes('text-align:right'));
 assert(report.indexOf('</table>')<report.indexOf('<h3>Next steps</h3>'),'tables must close before the next heading');
 assert(report.includes('<ol>')&&report.includes('<ul>'),'ordered and nested bullet lists must be readable');
 assert(!report.includes('<p><table'),'table blocks must not be nested inside paragraphs');
 assert(markdown('a | b\nordinary prose').startsWith('<p>'),'a pipe alone must not create a table');
 assert(markdown('3. First\n4. Second').includes('<ol start="3">'));
 for(const input of ['- First item\n  * * *','- First item\n  - - -','1. First item\n   - - -'])assert(markdown(input).includes('<hr>'),'a nested rule must terminate list parsing without looping');
 assert(markdown('> Prior observation\n> still needs checking').includes('<blockquote>'));
 const protectedCode=markdown('```html\n## Literal heading\n|a|b|\n|---|---|\n- literal list\n**literal emphasis**\n<img src=x onerror=alert(1)>\n```');
 assert(protectedCode.startsWith('<pre><code>'));
 assert(!/<(?:h[1-6]|table|li|strong|img)\b/.test(protectedCode),'fenced code must not be parsed as Markdown or raw HTML');
 assert(protectedCode.includes('&lt;img')&&protectedCode.includes('**literal emphasis**'));
 assert(markdown('```unsafe\nunclosed **literal**').endsWith('</code></pre>'),'unclosed fences must safely close at end of message');
 const inlineTable=markdown('| value | note |\n| --- | --- |\n| `a|b` | c\\|d |');
 assert.equal((inlineTable.match(/<th(?:\s[^>]*)?>/g)||[]).length,2,'pipes inside code and escaped pipes must not create extra columns');
 assert(inlineTable.includes('<code>a|b</code>')&&inlineTable.includes('c|d'));
 const inlineProtected=markdown('Use `<img **raw** | x>` then **real emphasis**.');
 assert(inlineProtected.includes('<code>&lt;img **raw** | x&gt;</code>'));
 assert(inlineProtected.includes('<strong>real emphasis</strong>'));
 const irregular=markdown('| a | b |\n| --- | --- |\n| short |\n| one | two | preserve this extra value |\n## Following section\n- next action');
 assert(irregular.includes('preserve this extra value'),'irregular tables must not discard data');
 assert(irregular.indexOf('</table>')<irregular.indexOf('<h2>Following section</h2>'));
 const attack=markdown('## <img src=x onerror=alert(1)>\n\n<script>alert(1)</script>\n\n[open](javascript:alert(1))\n\n| x | y |\n| --- | --- |\n| <svg onload=alert(1)> | &lt;img src=x&gt; |');
 assert(!/<(?:img|script|svg|iframe|a)\b/i.test(attack),'untrusted tags and dangerous links must stay inert');
 assert(!/\shref=|\sonerror=|\sonload=/i.test(attack.replace(/&lt;[^\n]*?&gt;/g,'')),'only renderer-owned attributes may be generated');
 assert(attack.includes('&amp;lt;img'),'pre-escaped input must not be decoded back into HTML');
 const tool=vm.runInContext('messageHtml('+JSON.stringify({id:7,role:'tool',tool_name:'request_logs',content:'## raw tool output\n|x|y|\n<img src=x>'})+')',sandbox);
 assert(tool.includes('<pre>')&&!tool.includes('<h2>')&&!tool.includes('<table>'),'tool receipts must remain raw preformatted evidence');
 console.log('PASS: business alerts, scoped multi-turn conversations, polling race, report selection, scoped two-step recovery, inconclusive evaluation, safe Markdown and XSS escaping.');

})().catch(error=>{console.error(error);process.exitCode=1});
