/* Synthetic DOM and transport fixtures only. No external API requests. */
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const html=fs.readFileSync('tradeflow/static/index.html','utf8');
const script=fs.readFileSync('tradeflow/static/document_jobs.js','utf8');
class Element {
  constructor(tag='div'){this.tagName=tag.toUpperCase();this.children=[];this.listeners={};this.dataset={};this.attributes={};this.value='';this.checked=false;this.hidden=false;this.disabled=false;this.textContent='';}
  append(...nodes){this.children.push(...nodes);}replaceChildren(...nodes){this.children=nodes;}get options(){return this.children;}get childElementCount(){return this.children.length;}
  setAttribute(name,value){this.attributes[name]=value;}removeAttribute(name){delete this.attributes[name];}querySelectorAll(selector){const rows=[];for(const node of this.children){if(!(node instanceof Element))continue;if(selector==='[data-group-member]:checked'&&node.dataset.groupMember&&node.checked)rows.push(node);if(selector==='[data-field-key]'&&node.dataset.fieldKey)rows.push(node);rows.push(...node.querySelectorAll(selector));}return rows;}addEventListener(name,callback){this.listeners[name]=callback;}focus(){}scrollIntoView(){}
}
function fixture(fetch){
  const ids=[...html.matchAll(/\bid="([^"]+)"/g)].map(match=>match[1]);
  const nodes=Object.fromEntries(ids.map(id=>[id,new Element()]));
  const customs=new Element('input');customs.checked=true;customs.value='customs';
  const document={getElementById:id=>{assert(nodes[id],`Missing control ${id}`);return nodes[id];},querySelector:()=>customs,querySelectorAll:selector=>selector.includes(':checked')?[customs]:[],createElement:tag=>new Element(tag),createTextNode:text=>({textContent:text})};
  let nextTimer=0;const timers=new Map(),durations=[];
  const context={document,location:{hash:'#SYNTHETIC'},window:{addEventListener(){}},structuredClone,AbortController,console,fetch,
    setTimeout(callback,ms){durations.push(ms);timers.set(++nextTimer,callback);return nextTimer;},clearTimeout(id){timers.delete(id);}};
  vm.createContext(context);
  vm.runInContext(script.replace(/\}\)\(\);\s*$/,'globalThis.proof={api,activate,model,loadCatalogStatus,loadCatalogDetail,searchCatalog,task,controls,requestBody,showPreview,renderFields,renderGroups,moveMembers,resetSource,saveCategory,applyCategory};})();'),context);
  return {context,nodes,customs,proof:context.proof,timers,durations,expire(){for(const [id,callback] of [...timers]){timers.delete(id);callback();}}};
}
const settle=()=>new Promise(resolve=>setImmediate(resolve));
const response=value=>({ok:true,json:async()=>value});
test('status request deadline aborts a stalled transport and releases its timer',async()=>{
  let signal;const f=fixture(async(_path,options)=>{signal=options?.signal;return new Promise(()=>{});});
  const pending=f.proof.api('/customs/hs-catalog/status');const rejected=assert.rejects(pending,/超时/);
  assert.deepEqual(f.durations,[5000]);f.expire();await rejected;assert.equal(signal.aborted,true);assert.equal(f.timers.size,0);
});
test('deadline covers JSON response body; HS queries use 25 seconds and ordinary API uses 30 seconds',async()=>{
  const f=fixture(async()=>({ok:true,json:()=>new Promise(()=>{})}));
  for(const [path,deadline] of [['/customs/hs-catalog/codes/0000000000',25000],['/customs/hs-catalog/search?keywords=SYNTHETIC',25000],['/customs/hs-catalog/updates?range=30d',25000],['/document-jobs',30000]]){
    const pending=f.proof.api(path);const rejected=assert.rejects(pending,/超时/);await settle();assert.equal(f.durations.at(-1),deadline);f.expire();await rejected;assert.equal(f.timers.size,0);
  }
});
test('successful responses and invalid JSON both clear the deadline',async()=>{
  const f=fixture(async()=>response({synthetic:true}));assert.equal((await f.proof.api('/document-jobs')).synthetic,true);assert.equal(f.timers.size,0);
  f.context.fetch=async()=>({ok:true,json:async()=>{throw Error('SYNTHETIC broken JSON');}});await assert.rejects(f.proof.api('/document-jobs'),/无法读取/);assert.equal(f.timers.size,0);
});
test('a failing job history never prevents the independent HS configuration check',async()=>{
  const calls=[];const f=fixture(async path=>{calls.push(path);if(path==='/document-jobs')throw Error('SYNTHETIC history failure');return response(path.endsWith('/status')?{provider:'SYNTHETIC',configured:true,https_only:true}:[]);});
  f.context.location.hash='#document-jobs';await f.proof.activate();assert(calls.includes('/customs/hs-catalog/status'));assert.match(f.nodes['document-hs-catalog-status'].textContent,/尚未验证/);assert.equal(f.proof.model.busy,false);assert.equal(f.timers.size,0);
});
test('a stalled source list does not keep the status check loading',async()=>{
  const f=fixture(async path=>path==='/document-jobs/sources'?new Promise(()=>{}):response(path.endsWith('/status')?{configured:false,https_only:true}:[]));
  f.context.location.hash='#document-jobs';const activation=f.proof.activate();await settle();assert.match(f.nodes['document-hs-catalog-status'].textContent,/尚未配置/);assert.equal(f.proof.model.busy,false);f.expire();await activation;assert.equal(f.timers.size,0);
});
test('manual configuration check has visible local loading, releases on timeout, and does not claim provider availability',async()=>{
  const f=fixture(async()=>new Promise(()=>{}));const pending=f.nodes['document-hs-catalog-status-refresh'].listeners.click({currentTarget:f.nodes['document-hs-catalog-status-refresh']});
  assert.equal(f.proof.model.busy,false);assert.equal(f.nodes['document-hs-catalog-status-refresh'].disabled,true);assert.match(f.nodes['document-hs-catalog-status-refresh'].textContent,/检查中/);f.expire();await pending;
  assert.equal(f.nodes['document-hs-catalog-status-refresh'].disabled,false);assert.match(f.nodes['document-hs-catalog-status'].textContent,/超时/);assert.equal(f.timers.size,0);
});
test('HS search timeout restores the query button and global document controls',async()=>{
  const f=fixture(async()=>new Promise(()=>{}));f.proof.model.catalogStatus={configured:true};f.nodes['document-hs-catalog-keywords'].value='SYNTHETIC inquiry';f.proof.controls();
  const button=f.nodes['document-hs-catalog-search'];const pending=button.listeners.click({currentTarget:button});assert.equal(f.proof.model.busy,true);assert.equal(button.disabled,true);assert.equal(button.attributes['aria-busy'],'true');f.expire();await pending;
  assert.equal(f.proof.model.busy,false);assert.equal(button.disabled,false);assert.equal(button.attributes['aria-busy'],undefined);assert.match(f.nodes['document-hs-catalog-message'].textContent,/超时/);assert.match(f.nodes['document-status'].textContent,/超时/);assert.equal(f.timers.size,0);
});
test('MCP configuration labels its protocol and blocks unsupported update requests even after controls rerender',async()=>{
  const calls=[];const f=fixture(async path=>{calls.push(path);return response({provider:'hsciq_mcp',protocol:'mcp',configured:true,https_only:true,capabilities:{search:true,detail:true,updates:false}});});
  await f.proof.loadCatalogStatus();assert.match(f.nodes['document-hs-catalog-status'].textContent,/HSCIQ.*MCP/);assert.match(f.nodes['document-hs-catalog-status'].textContent,/尚未验证/);
  f.proof.model.busy=true;f.proof.controls();f.proof.model.busy=false;f.proof.controls();assert.equal(f.nodes['document-hs-catalog-updates'].disabled,true);assert.match(f.nodes['document-hs-catalog-updates-note'].textContent,/MCP.*未提供编码变更记录/);
  await f.nodes['document-hs-catalog-updates'].listeners.click({currentTarget:f.nodes['document-hs-catalog-updates']});assert.deepEqual(calls,['/customs/hs-catalog/status']);assert.doesNotMatch(f.nodes['document-hs-catalog-updates-summary'].textContent,/0 条|鉴权/);assert.equal(f.proof.model.busy,false);
});
test('legacy statuses without capabilities keep tax REST update queries available',async()=>{
  const calls=[];const f=fixture(async path=>{calls.push(path);return response(path.endsWith('/status')?{provider:'hsciq',protocol:'tax_rest',configured:true,https_only:true}:{items:[],total_count:0});});
  await f.proof.loadCatalogStatus();assert.match(f.nodes['document-hs-catalog-status'].textContent,/税则 REST/);assert.equal(f.nodes['document-hs-catalog-updates'].disabled,false);assert.equal(f.nodes['document-hs-catalog-updates-note'].hidden,true);
  await f.nodes['document-hs-catalog-updates'].listeners.click({currentTarget:f.nodes['document-hs-catalog-updates']});assert(calls.some(path=>path.startsWith('/customs/hs-catalog/updates?')));
});
test('explicit search and detail capabilities stay disabled across normal control updates',async()=>{
  const f=fixture(async()=>response({provider:'SYNTHETIC',configured:true,capabilities:{search:false,detail:false,updates:true}}));await f.proof.loadCatalogStatus();f.proof.controls();
  for(const id of ['document-hs-catalog-search','document-hs-catalog-next','document-hs-catalog-detail-button','document-hs-catalog-refresh'])assert.equal(f.nodes[id].disabled,true,id);assert.equal(f.nodes['document-hs-catalog-updates'].disabled,false);
});
test('configuration check never claims service success; detail feedback distinguishes fresh service data from cache',async()=>{
  let cacheHit=false;const f=fixture(async path=>response(path.endsWith('/status')?{provider:'hsciq_mcp',protocol:'mcp',configured:true}:{provider:'hsciq_mcp',hs_code:'0000000000',snapshot_id:'SYNTHETIC-snapshot',status:'active',cache_hit:cacheHit,reporting_elements:[]}));
  await f.proof.loadCatalogStatus();assert.doesNotMatch(f.nodes['document-hs-catalog-status'].textContent,/本次服务查询成功/);await f.proof.loadCatalogDetail('0000000000');assert.match(f.nodes['document-hs-catalog-status'].textContent,/HSCIQ.*MCP.*本次服务查询成功/);
  cacheHit=true;await f.proof.loadCatalogDetail('0000000000');assert.match(f.nodes['document-hs-catalog-status'].textContent,/已读取目录缓存；缓存不代表本次鉴权/);assert.doesNotMatch(f.nodes['document-hs-catalog-status'].textContent,/本次服务查询成功/);
  await f.proof.loadCatalogStatus();assert.match(f.nodes['document-hs-catalog-status'].textContent,/尚未验证/);assert.doesNotMatch(f.nodes['document-hs-catalog-status'].textContent,/本次服务查询成功/);
});
test('successful live search reports that this provider query passed',async()=>{
  const f=fixture(async path=>response(path.endsWith('/status')?{provider:'hsciq_mcp',protocol:'mcp',configured:true}:{provider:'hsciq_mcp',items:[],has_more:false}));await f.proof.loadCatalogStatus();f.nodes['document-hs-catalog-keywords'].value='SYNTHETIC';await f.proof.searchCatalog();assert.match(f.nodes['document-hs-catalog-status'].textContent,/HSCIQ.*MCP.*本次服务查询成功/);assert.equal(f.timers.size,0);
});

function syntheticGroups(){const members=[{line_id:'SYNTHETIC-A',product_name:'合成螺钉',specification:'M2×4',material:'合成钢',grade:'SYNTHETIC',surface_color:'黑锌',quantity:'10',unit:'支'},{line_id:'SYNTHETIC-B',product_name:'合成螺钉',specification:'M3×5',material:'合成钢',grade:'SYNTHETIC',surface_color:'镀镍',quantity:'20',unit:'支'}];return [{group_id:'SYNTHETIC-group',name:'合成测试类别',product_name:'合成螺钉',material:'合成钢',grade:'SYNTHETIC',diameter_min:null,diameter_max:'6',min_inclusive:true,max_inclusive:false,member_line_ids:members.map(row=>row.line_id),members,fields:{hs_code:'0000000000'},quantity:'30',amount_usd:'1.00',unit_price_usd:'0.0333',rounding_difference_usd:'-0.001',surface_colors:['黑锌','镀镍']}];}
function groupedFixture(){const f=fixture(async()=>response([]));f.nodes['document-source'].value='SYNTHETIC-source';f.proof.model.sources=[{source_id:'SYNTHETIC-source',filename:'SYNTHETIC.xlsx',items:syntheticGroups()[0].members}];f.nodes['document-rate'].value='7';return f;}
test('first customs preview requests automatic grouping, while unselected customs sends no grouping',()=>{
  const f=groupedFixture();assert.deepEqual(JSON.parse(JSON.stringify(f.proof.requestBody().customs_groups)),[]);f.context.document.querySelectorAll=()=>[];assert.equal(f.proof.requestBody().customs_groups,null);
});
test('preview adopts category groups and request carries only editable classification facts',()=>{
  const f=groupedFixture();f.proof.showPreview({conversion:{items:[],totals:{}},documents:[],customs_groups:syntheticGroups(),field_specs:[]});const body=JSON.parse(JSON.stringify(f.proof.requestBody()));assert.equal(body.customs_groups.length,1);assert.deepEqual(body.customs_groups[0].member_line_ids,['SYNTHETIC-A','SYNTHETIC-B']);assert.equal(body.customs_groups[0].quantity,undefined);assert.equal(body.customs_groups[0].amount_usd,undefined);assert.equal(body.customs_groups[0].fields.legal_quantity,undefined);assert.deepEqual(body.item_extras,{});assert.equal(f.nodes['document-hs-line'].options.length,2);
});
test('category fields remain independent from original CI product supplements',()=>{
  const f=groupedFixture();f.proof.model.customsGroups=syntheticGroups();f.proof.model.item_extras={'SYNTHETIC-A':{description:'SYNTHETIC CI description'}};f.proof.renderFields([{scope:'items',line_id:'SYNTHETIC-group',key:'description',label:'商品名称规格',value:'SYNTHETIC derived default',missing:true}]);const inputs=f.nodes['document-fields'].querySelectorAll('[data-field-key]');assert.equal(inputs.length,1);assert.equal(f.proof.model.customsGroups[0].fields.description,undefined);inputs[0].value='合成分类申报内容';inputs[0].listeners.input();assert.equal(f.proof.model.customsGroups[0].fields.description,'合成分类申报内容');assert.equal(f.proof.model.item_extras['SYNTHETIC-A'].description,'SYNTHETIC CI description');assert.equal(f.proof.model.item_extras['SYNTHETIC-group'],undefined);
});
test('HS directory application requires previewed groups and then binds group instead of ERP rows',()=>{
  const f=groupedFixture();f.proof.model.catalogDetail={hs_code:'0000000000',snapshot_id:'SYNTHETIC-snapshot',status:'active'};f.nodes['document-hs-catalog-line'].value='*';f.nodes['document-hs-catalog-apply'].listeners.click();assert.match(f.nodes['document-status'].textContent,/先.*预览/);assert.deepEqual(JSON.parse(JSON.stringify(f.proof.model.item_extras)),{});f.proof.model.customsGroups=syntheticGroups();f.nodes['document-hs-catalog-apply'].listeners.click();assert.equal(f.proof.model.customsGroups[0].catalog_snapshot_id,'SYNTHETIC-snapshot');assert.deepEqual(JSON.parse(JSON.stringify(f.proof.model.item_extras)),{});assert.deepEqual(JSON.parse(JSON.stringify(f.proof.model.hs_catalog_bindings)),{});
});
test('splitting members preserves exact coverage and source supplements, clears confirmations and class net weights',()=>{
  const f=groupedFixture();f.proof.model.customsGroups=syntheticGroups();f.proof.model.customsGroups[0].fields.legal_quantity='1.2';f.proof.model.customs_confirmations={SYNTHETIC:'token'};f.proof.model.item_extras={'SYNTHETIC-A':{description:'SYNTHETIC CI'}};f.proof.renderGroups();const checks=[];function walk(node){for(const child of node.children||[]){if(child.dataset?.groupMember)checks.push(child);walk(child);}}walk(f.nodes['document-groups-list']);checks[0].checked=true;f.nodes['document-group-target'].value='';f.proof.moveMembers();assert.equal(f.proof.model.customsGroups.length,2);const ids=f.proof.model.customsGroups.flatMap(group=>group.member_line_ids);assert.deepEqual([...ids].sort(),['SYNTHETIC-A','SYNTHETIC-B']);assert.equal(new Set(ids).size,2);assert(f.proof.model.customsGroups.every(group=>group.fields.legal_quantity===undefined));assert.deepEqual(JSON.parse(JSON.stringify(f.proof.model.customs_confirmations)),{});assert.equal(f.proof.model.groupsDirty,true);assert.equal(f.proof.model.item_extras['SYNTHETIC-A'].description,'SYNTHETIC CI');assert.match(f.nodes['document-groups-summary'].textContent,/重新预览/);
});
test('source reset discards prior categories, bindings, rate and confirmations',()=>{
  const f=groupedFixture();f.proof.model.customsGroups=syntheticGroups();f.proof.model.customs_confirmations={SYNTHETIC:'token'};f.proof.model.hs_catalog_bindings={SYNTHETIC:'old'};f.proof.resetSource();assert.equal(f.proof.model.customsGroups,null);assert.equal(f.nodes['document-rate'].value,'');assert.deepEqual(JSON.parse(JSON.stringify(f.proof.model.customs_confirmations)),{});assert.deepEqual(JSON.parse(JSON.stringify(f.proof.model.hs_catalog_bindings)),{});assert.equal(f.nodes['document-hs-line'].options.length,1);
});
test('moving members into an existing group preserves all product identities once and invalidates old totals',()=>{
  const f=groupedFixture(),first=syntheticGroups()[0];f.proof.model.customsGroups=[{...structuredClone(first),member_line_ids:['SYNTHETIC-A'],members:[first.members[0]]},{...structuredClone(first),group_id:'SYNTHETIC-second',member_line_ids:['SYNTHETIC-B'],members:[first.members[1]]}];f.proof.renderGroups();let chosen;function walk(node){for(const child of node.children||[]){if(child.dataset?.groupMember==='SYNTHETIC-group')chosen=child;walk(child);}}walk(f.nodes['document-groups-list']);chosen.checked=true;f.nodes['document-group-target'].value='SYNTHETIC-second';f.proof.moveMembers();assert.equal(f.proof.model.customsGroups.length,1);assert.equal(f.proof.model.customsGroups[0].group_id,'SYNTHETIC-second');assert.deepEqual([...f.proof.model.customsGroups[0].member_line_ids].sort(),['SYNTHETIC-A','SYNTHETIC-B']);assert.equal(f.proof.model.groupsDirty,true);
});
test('saving a renamed category creates a new rule while preserving source predicate and explicit bounds',async()=>{
  const f=groupedFixture();f.proof.model.customsGroups=syntheticGroups();f.proof.model.customsGroups[0].category_id='SYNTHETIC-old';f.proof.model.categories=[{id:'SYNTHETIC-old',name:'原合成类别',version:8}];f.proof.renderGroups();f.nodes['document-category-name'].value='新合成类别';f.nodes['document-category-hs'].value='0000000000';f.nodes['document-category-max'].value='6';f.nodes['document-category-max-inclusive'].checked=false;f.nodes['document-category-reason'].value='SYNTHETIC test only';let savedBody;f.context.fetch=async(path,options)=>{if(options?.method==='POST'){savedBody=JSON.parse(options.body);return response({id:'SYNTHETIC-new',version:1,...savedBody});}return response([{id:'SYNTHETIC-new',version:1,...savedBody}]);};await f.proof.saveCategory();assert.equal(savedBody.expected_version,0);assert.equal(savedBody.product_name,'合成螺钉');assert.equal(savedBody.material,'合成钢');assert.equal(savedBody.grade,'SYNTHETIC');assert.equal(savedBody.diameter_max,'6');assert.equal(savedBody.max_inclusive,false);assert.equal(savedBody.legal_quantity,undefined);assert.equal(savedBody.quantity,undefined);assert.equal(f.proof.model.customsGroups[0].category_id,'SYNTHETIC-new');assert.equal(f.proof.model.groupsDirty,true);
});
test('empty optional category defaults preserve this job values and do not create explicit blank overrides',()=>{
  const f=groupedFixture();f.proof.model.customsGroups=syntheticGroups();f.proof.renderGroups();const category={id:'SYNTHETIC-category',name:'合成类别',hs_code:'0000000000',description:'',declaration_elements_text:'',origin_country:'',legal_unit:'KG'};f.proof.model.categories=[category];f.nodes['document-category-select'].value=category.id;f.proof.applyCategory();let fields=f.proof.model.customsGroups[0].fields;assert.equal(Object.hasOwn(fields,'declaration_elements_text'),false);assert.equal(Object.hasOwn(fields,'description'),false);assert.equal(Object.hasOwn(fields,'origin_country'),false);fields.declaration_elements_text='合成本票申报要素';f.proof.applyCategory();assert.equal(fields.declaration_elements_text,'合成本票申报要素');fields.declaration_elements_text='';f.proof.applyCategory();assert.equal(Object.hasOwn(fields,'declaration_elements_text'),true);assert.equal(fields.declaration_elements_text,'');
});
test('saving empty optional category elements does not override template defaults or erase this job explicit edits',async()=>{
  for(const initial of [undefined,'合成本票已有要素','']){const f=groupedFixture();f.proof.model.customsGroups=syntheticGroups();if(initial!==undefined)f.proof.model.customsGroups[0].fields.declaration_elements_text=initial;f.proof.renderGroups();f.nodes['document-category-name'].value='合成空默认测试';f.nodes['document-category-reason'].value='SYNTHETIC regression';f.nodes['document-category-elements'].value='';let saved;f.context.fetch=async(_path,options)=>{if(options?.method==='POST'){saved={...JSON.parse(options.body),id:'SYNTHETIC-category',version:1};return response(saved);}return response([saved]);};await f.proof.saveCategory();const fields=f.proof.model.customsGroups[0].fields;assert.equal(Object.hasOwn(fields,'declaration_elements_text'),initial!==undefined);assert.equal(fields.declaration_elements_text,initial);}
});
