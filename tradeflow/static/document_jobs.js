/* Local document workflow. All monetary calculations remain on the server. */
(() => {
  'use strict';
  const el = id => document.getElementById(id);
  const labels = {ci:'CI 商业发票',pi:'PI 形式发票',pl:'PL 装箱单',customs:'报关单',receipt:'发货签收单'};
  const model = {sources:[],fields:{},item_extras:{},customsGroups:null,groupsDirty:false,categories:[],customs_confirmations:{},review:null,hsFormats:[],hs_catalog_bindings:{},catalogStatus:null,catalogStatusLoading:false,catalogDetail:null,catalogPage:1,catalogKeywords:"",preview:null,busy:false};
  const own = (obj,key) => Object.prototype.hasOwnProperty.call(obj,key);
  function say(message,error=false) { const node=el('document-status');node.textContent=message;node.className='status'+(error?' error':''); }
  async function api(path,options={}) {
    const route=path.split('?')[0];
    const deadline=route==='/customs/hs-catalog/status'?5000:route.startsWith('/customs/hs-catalog/')?25000:30000;
    const controller=new AbortController();let expired=false;
    let timer;
    const deadlinePromise=new Promise((_,reject)=>{timer=setTimeout(()=>{expired=true;reject(new Error('请求超时（'+deadline/1000+' 秒），请检查本机服务或网络后重试。'));controller.abort();},deadline);});
    const request=async()=>{
      let response;try{response=await fetch(path,{...options,signal:controller.signal});}catch(error){if(controller.signal.aborted)throw error;throw new Error('无法连接本机服务，请确认服务已启动。');}
      let data;try{data=await response.json();}catch(error){if(controller.signal.aborted)throw error;throw new Error('服务返回的内容无法读取，请稍后重试。');}
      if(!response.ok)throw new Error(typeof data.detail==='string'?data.detail:JSON.stringify(data.detail||data));
      return data;
    };
    try{return await Promise.race([request(),deadlinePromise]);}
    catch(error){if(expired)throw new Error('请求超时（'+deadline/1000+' 秒），请检查本机服务或网络后重试。');throw error;}
    finally{clearTimeout(timer);}
  }
  function groupRequest(group){return {group_id:group.group_id,member_line_ids:[...group.member_line_ids],category_id:group.category_id||null,fields:{...(group.fields||{})},catalog_snapshot_id:group.catalog_snapshot_id||null};}
  function requestBody(){const types=[...document.querySelectorAll('[name=document-type]:checked')].map(node=>node.value);return {source_id:el('document-source').value,exchange_rate:el('document-rate').value.trim(),document_types:types,fields:{...model.fields},item_extras:structuredClone(model.item_extras),document_fields:{},customs_confirmations:{...model.customs_confirmations},hs_catalog_bindings:{...model.hs_catalog_bindings},customs_groups:types.includes('customs')?(model.customsGroups||[]).map(groupRequest):null};}
  function groupFor(lineId){return model.customsGroups?.find(group=>group.group_id===lineId);}
  function customsTargets(scope){return (model.customsGroups===null?source()?.items||[]:model.customsGroups).filter(row=>scope==='*'||scope===(row.group_id||row.line_id));}
  function groupMode(){return document.querySelector('[name=document-type][value=customs]').checked;}
  function requireGroups(){if(groupMode()&&!model.customsGroups?.length){say('请先填写汇率并预览，生成本次申报分类后再应用。',true);return false;}return true;}
  function itemValues(spec){return groupFor(spec.line_id)?.fields||(model.item_extras[spec.line_id]||{});}
  function catalogCapability(name){return model.catalogStatus?.capabilities?.[name]!==false;}
  function catalogProviderLabel(provider,protocol){
    if(provider==='hsciq_mcp'||protocol==='mcp')return 'HSCIQ · MCP';
    if(provider==='hsciq'||protocol==='tax_rest')return 'HSCIQ · 税则 REST';
    return provider||'目录服务';
  }
  function renderCatalogCapabilities(){
    const note=el('document-hs-catalog-updates-note');const supported=catalogCapability('updates');note.hidden=supported;
    note.textContent=supported?'':(model.catalogStatus?.protocol==='mcp'||model.catalogStatus?.provider==='hsciq_mcp'?'当前 MCP 接口未提供编码变更记录；可重新查询详情核对更新时间。':'当前接口未提供编码变更记录；可重新查询详情核对更新时间。');
    if(!supported){el('document-hs-catalog-updates-summary').textContent='';el('document-hs-catalog-updates-list').replaceChildren();}
  }
  function controls(){
    for(const node of el('document-jobs').querySelectorAll('input,select,textarea,button'))node.disabled=model.busy;
    el('document-generate').disabled=model.busy||!model.preview?.ready;
    el('document-preview-button').disabled=model.busy||!el('document-source').value;
    el('document-jobs').setAttribute('aria-busy',String(model.busy));
    for(const check of el('document-fields').querySelectorAll('[data-empty-target]'))if(check.checked)el(check.dataset.emptyTarget).disabled=true;
    for(const check of el('document-customs-review-items').querySelectorAll('[data-confirmation-blocked]'))check.disabled=true;
    el('document-hs-apply').disabled=model.busy||!source();
    const configured=model.catalogStatus?.configured===true;
    for(const id of ['document-hs-catalog-search','document-hs-catalog-next'])el(id).disabled=model.busy||!configured||!catalogCapability('search');
    el('document-hs-catalog-refresh').disabled=model.busy||!configured||!catalogCapability('detail');
    el('document-hs-catalog-detail-button').disabled=model.busy||!catalogCapability('detail');
    for(const id of ['document-hs-catalog-updates','document-hs-catalog-range'])el(id).disabled=model.busy||!configured||!catalogCapability('updates');
    renderCatalogCapabilities();
    el('document-hs-catalog-apply').disabled=model.busy||!source()||!model.catalogDetail?.snapshot_id||model.catalogDetail.status==='expired'||model.catalogDetail.stale===true;
    el('document-hs-catalog-unbind').disabled=model.busy||!source();
    el('document-hs-catalog-status-refresh').disabled=model.busy||model.catalogStatusLoading;
  }
  function invalidate(message='内容已调整，请重新预览后生成。',keepConfirmations=false){
    const hadPreview=!!model.preview;model.preview=null;el('document-preview').hidden=true;el('document-hs-catalog-review').hidden=true;el('document-generate').disabled=true;
    if(!keepConfirmations){model.customs_confirmations={};model.review=null;el('document-customs-review').hidden=true;}
    if(hadPreview)say(message);
  }
  async function task(button,work){if(model.busy)return;model.busy=true;button.setAttribute('aria-busy','true');controls();try{await work();}catch(error){say(error.message,true);}finally{model.busy=false;button.removeAttribute('aria-busy');controls();}}
  function addText(parent,tag,value,className){const node=document.createElement(tag);node.textContent=value==null?'—':String(value);if(className)node.className=className;parent.append(node);return node;}
  function makeTable(parent,columns,rows,caption){
    parent.replaceChildren();if(!rows.length){addText(parent,'p','暂无明细。','empty');return;}
    const table=document.createElement('table');addText(table,'caption',caption);const head=document.createElement('thead'),tr=document.createElement('tr');
    for(const [name] of columns){const th=addText(tr,'th',name);th.scope='col';}head.append(tr);table.append(head);
    const body=document.createElement('tbody');for(const row of rows){const line=document.createElement('tr');for(const [,read,wrap] of columns)addText(line,'td',typeof read==='function'?read(row):row[read],wrap?'wrap':'');body.append(line);}table.append(body);parent.append(table);
  }
  function source(){return model.sources.find(row=>row.source_id===el('document-source').value);}
  function showSource(){
    const row=source();el('document-source-info').hidden=!row;
    if(!row)return;
    const header=row.header||{};el('document-source-summary').textContent=[row.filename,header.order_no&&'合同/订单号：'+header.order_no,header.buyer_name&&'客户：'+header.buyer_name,'原币：人民币 CNY',row.items?.length+' 项产品','人民币合计：'+(row.totals?.total_cny??'未提供')].filter(Boolean).join(' · ');
    makeTable(el('document-source-table'),[['品名','product_name',true],['规格','specification',true],['数量','quantity'],['单位','unit'],['表色','surface_color'],['等级','grade'],['人民币单价','unit_price_cny'],['人民币金额','amount_cny']],row.items||[],'ERP 原始产品和人民币价格；核对后填写本次汇率。');
  }
  async function loadSources(selected=el('document-source').value){
    const rows=await api('/document-jobs/sources');model.sources=Array.isArray(rows)?rows:rows.sources||[];const select=el('document-source');select.replaceChildren();
    const empty=document.createElement('option');empty.value='';empty.textContent='请选择 ERP Excel';select.append(empty);
    for(const row of model.sources){const option=document.createElement('option');option.value=row.source_id;option.textContent=row.filename+(row.header?.order_no?' · '+row.header.order_no:'');select.append(option);}
    select.value=model.sources.some(row=>row.source_id===selected)?selected:'';showSource();controls();
  }
  function resetSource(){model.fields={};model.item_extras={};model.customsGroups=null;model.groupsDirty=false;model.hs_catalog_bindings={};el('document-hs-catalog-review').hidden=true;el('document-rate').value='';el('document-fields').replaceChildren();el('document-supplements').hidden=true;invalidate('已切换 ERP 来源，请重新填写本次汇率并预览。');showSource();renderGroups();renderHsLines();controls();}
  function missingText(missing){return (missing||[]).map(value=>{
    if(typeof value!=='string')return value.label||value.message||'需要补充信息';
    const spec=(model.preview?.field_specs||[]).find(row=>value===(row.scope==='items'?'items.'+row.line_id+'.':'fields.')+row.key);
    if(!spec)return value.startsWith('template_')?'默认模板容量不足，请在管理中检查模板':'需要补充信息';
    const item=spec.scope==='items'?source()?.items?.find(row=>row.line_id===spec.line_id):null;
    return (item?item.specification+'：':'')+(spec.label||'资料');
  }).join('；');}
  function renderFields(specs){
    specs=specs.filter(spec=>!spec.readonly);const parent=el('document-fields');parent.replaceChildren();el('document-supplements').hidden=!specs.length;
    const optional=document.createElement('details');optional.className='document-optional-fields';
    const optionalTitle=document.createElement('summary');optionalTitle.textContent='模板常用内容（可修改）';optional.append(optionalTitle);
    const optionalFields=document.createElement('div');optionalFields.className='supplement-grid';optional.append(optionalFields);
    specs.forEach((spec,index)=>{
      const values=spec.scope==='items'?itemValues(spec):model.fields;
      const current=own(values,spec.key)?values[spec.key]:spec.value;
      const holder=document.createElement('div');holder.className='document-field';
      const label=document.createElement('label'),id='document-extra-'+index;label.htmlFor=id;
      const item=spec.scope==='items'?source()?.items?.find(item=>item.line_id===spec.line_id):null;
      const group=groupFor(spec.line_id);label.textContent=(group?(group.name||group.product_name||'申报类别')+' · ':item?[item.product_name,item.specification].filter(Boolean).join(' / ')+' · ':'')+(spec.label||spec.key)+(spec.missing?'（待补充）':'');
      const input=document.createElement(spec.input_type==='textarea'?'textarea':'input');input.id=id;input.name=id;if(input.tagName==='INPUT')input.type=spec.input_type==='date'?'date':'text';
      if(spec.input_type==='decimal')input.inputMode='decimal';input.value=current==null?'':String(current);input.setAttribute('aria-describedby',id+'-help document-status');
      input.readOnly=!!spec.readonly;input.dataset.fieldScope=spec.scope;input.dataset.fieldKey=spec.key;if(spec.line_id)input.dataset.lineId=spec.line_id;
      const save=value=>{if(spec.scope==='items'){if(group){group.fields??={};group.fields[spec.key]=value;if(spec.key==='hs_code'){group.catalog_snapshot_id=null;group.category_id=null;}}else{model.item_extras[spec.line_id]??={};model.item_extras[spec.line_id][spec.key]=value;if(spec.key==='hs_code')delete model.hs_catalog_bindings[spec.line_id];}}else model.fields[spec.key]=value;invalidate();};
      if(!spec.readonly)input.addEventListener('input',()=>save(input.value));holder.append(label,input);
      const help=addText(holder,'p',[(spec.red?'红字字段，每次核对。':''),(spec.readonly?'由 ERP 或本次换算提供，只读。':''),'用于：'+(spec.documents||[]).map(type=>labels[type]||type).join('、'),spec.source?'来源：'+sourceText(spec.source):''].filter(Boolean).join(' '),'hint');help.id=id+'-help';
      if(spec.allow_empty&&!spec.readonly){const emptyLabel=document.createElement('label');emptyLabel.className='inline-choice';const check=document.createElement('input');check.type='checkbox';check.dataset.emptyTarget=id;check.checked=own(values,spec.key)&&values[spec.key]==='';check.setAttribute('aria-label','确认'+(spec.label||spec.key)+'留空');check.addEventListener('change',()=>{input.disabled=check.checked;save(check.checked?'':input.value);});emptyLabel.append(check,document.createTextNode('确认此项留空'));holder.append(emptyLabel);if(check.checked)input.disabled=true;}
      (spec.show_primary||spec.missing||own(values,spec.key)?parent:optionalFields).append(holder);
    });
    if(optionalFields.childElementCount)parent.append(optional);
  }
  function sourceText(value){
    if(value==null)return '本次填写或模板内容';
    if(typeof value==='string')return value;
    if(Array.isArray(value))return value.map(sourceText).join('；');
    return [value.label,value.filename,value.sheet,value.cell||value.source_cell,value.row&&'行 '+value.row,value.type||value.kind].filter(Boolean).join(' · ')||JSON.stringify(value);
  }
  function reviewProgress(){
    const items=model.review?.items||[];const count=items.filter(item=>item.token&&model.customs_confirmations[item.id]===item.token).length;
    el('document-customs-progress').textContent='本次已核对 '+count+' / '+items.length+' 项。'+(count===items.length?(model.preview?.ready?'当前预览核对已完成，可以生成所选单据。':'请再次点击“预览换算与单据缺项”检查，再生成。'):'请逐项核对实际值及来源，原值正确时直接确认即可。');
  }
  function renderCustomsReview(review){
    model.review=review||null;const parent=el('document-customs-review-items');parent.replaceChildren();el('document-customs-review').hidden=!review?.items?.length;
    if(!review?.items?.length)return;
    const valid={};for(const item of review.items)if(item.token&&(item.confirmed||model.customs_confirmations[item.id]===item.token))valid[item.id]=item.token;model.customs_confirmations=valid;
    for(const item of review.items){
      const row=document.createElement('div');row.className='customs-review-item';
      const content=document.createElement('div');const product=item.line_id?(groupFor(item.line_id)||source()?.items?.find(line=>line.line_id===item.line_id)):null;
      const productLabel=product?.name||product?.specification;addText(content,'strong',(productLabel?productLabel+' · ':'')+item.label);
      if(item.red)addText(content,'span','红字 · 每次核对','customs-review-tag');else if(item.core)addText(content,'span','本次必核对','customs-review-tag');
      addText(content,'p',item.value==null?'待补齐，补充后重新预览':item.value===''?'（留空）':item.value,'customs-review-value');addText(content,'p','来源：'+sourceText(item.source),'hint');
      if(item.editable){const edit=document.createElement('button');edit.type='button';edit.className='secondary';edit.textContent='修改此项';edit.addEventListener('click',()=>{const inputs=[...el('document-fields').querySelectorAll('[data-field-key]')];const keys=item.key==='quantity_and_units'?['legal_quantity','net_weight']:[item.key];let input;for(const key of keys){input=inputs.find(node=>node.dataset.fieldScope===item.scope&&node.dataset.fieldKey===key&&(node.dataset.lineId||'')===(item.line_id||''));if(input)break;}if(!input&&item.key==='quantity_and_units'&&model.customsGroups?.length===1)input=inputs.find(node=>node.dataset.fieldScope==='fields'&&node.dataset.fieldKey==='net_weight');if(input){const details=input.closest('details');if(details)details.open=true;input.focus();input.scrollIntoView({block:'center',behavior:'smooth'});}else say('请在下方补充信息中修改此项，修改后重新预览。');});content.append(edit);}
      else addText(content,'p','原始 ERP 或换算结果，只读核对。','hint');
      const label=document.createElement('label');label.className='inline-choice';const check=document.createElement('input');check.type='checkbox';check.checked=!!item.token&&model.customs_confirmations[item.id]===item.token;if(!item.token){check.disabled=true;check.dataset.confirmationBlocked='true';}check.setAttribute('aria-label','已核对 '+(productLabel?productLabel+' ':'')+item.label);check.addEventListener('change',()=>{if(!item.token)return;if(check.checked)model.customs_confirmations[item.id]=item.token;else delete model.customs_confirmations[item.id];invalidate('核对内容已记录，请重新预览后生成。',true);reviewProgress();say('核对内容已记录，请重新预览后生成。');});label.append(check,document.createTextNode(item.token?'已核对':'先补齐'));row.append(content,label);parent.append(row);
    }
    reviewProgress();
  }
  function renderHsLines(){for(const id of ['document-hs-line','document-hs-catalog-line']){const select=el(id);const previous=select.value;select.replaceChildren();const all=document.createElement('option');all.value='*';all.textContent=model.customsGroups===null?'先预览，生成申报分类':'应用到本次全部申报类别';select.append(all);for(const row of model.customsGroups||[]){const option=document.createElement('option');option.value=row.group_id;option.textContent=row.name||[row.product_name,row.material,row.grade,row.diameter_label].filter(Boolean).join(' / ');select.append(option);}if([...select.options].some(option=>option.value===previous))select.value=previous;}}
  function selectedGroup(){return groupFor(el('document-group-selected').value);}
  function groupTitle(group){return group.name||[group.product_name,group.material,group.grade,group.diameter_label].filter(Boolean).join(' / ')||'待确认类别';}
  function categoryEditor(){
    const group=selectedGroup(),fields=group?.fields||{};
    el('document-category-condition').textContent=group?'匹配条件：'+[group.product_name,group.material,group.grade].filter(Boolean).join(' / ')+'。品名、材质、等级来自 ERP，保存前请核对杆径范围。':'先预览生成类别，再选择当前类别。';
    for(const [id,value] of [['name',group?.name||''],['hs',fields.hs_code||''],['min',group?.diameter_min??''],['max',group?.diameter_max??''],['description',fields.description||''],['elements',fields.declaration_elements_text||''],['reason','']])el('document-category-'+id).value=String(value);
    el('document-category-min-inclusive').checked=group?.min_inclusive??group?.diameter_min_inclusive??true;el('document-category-max-inclusive').checked=group?.max_inclusive??group?.diameter_max_inclusive??false;
  }
  function renderGroups(){
    const groups=model.customsGroups||[],parent=el('document-groups-list');parent.replaceChildren();
    el('document-groups-summary').textContent=groups.length?'本次 '+groups.length+' 个申报类别，包含 '+groups.reduce((count,group)=>count+group.member_line_ids.length,0)+' 行 ERP 产品。'+(model.groupsDirty?'分类或汇率已调整，请重新预览计算汇总。':'每类对应报关单一项，展开可核对成员。'):'先填写本次汇率并预览，系统会按已保存类别和 ERP 信息建议分组；请核对分类范围。';
    const current=el('document-group-selected').value;for(const id of ['document-group-selected','document-group-target'])el(id).replaceChildren();const split=document.createElement('option');split.value='';split.textContent='拆为新类别';el('document-group-target').append(split);
    for(const group of groups){
      for(const id of ['document-group-selected','document-group-target']){const option=document.createElement('option');option.value=group.group_id;option.textContent=groupTitle(group);el(id).append(option);}
      const block=document.createElement('section');block.className='customs-group';addText(block,'h5',groupTitle(group));
      addText(block,'p',[group.fields?.hs_code?'HS '+group.fields.hs_code:'HS 待确认',group.material,group.grade,group.diameter_label,'表色：'+((group.surface_colors||[]).join('、')||'未提供')].filter(Boolean).join(' · '),'hint');
      addText(block,'p',model.groupsDirty?'汇总待重新预览':'数量 '+(group.quantity??'—')+' '+(group.unit||'')+' · 金额 '+(group.amount_usd??'—')+' USD · 倒算单价 '+(group.unit_price_usd??'—')+' USD');
      if(!model.groupsDirty)addText(block,'p','总金额按成员金额累加。总金额减去展示单价 × 数量的差额：'+(group.rounding_difference_usd??'—')+' USD；不据此改动总金额。','hint');
      const details=document.createElement('details');addText(details,'summary','查看 '+group.member_line_ids.length+' 行原始成员 / 勾选调整分类');
      for(const lineId of group.member_line_ids){const line=(group.members||[]).find(row=>row.line_id===lineId)||source()?.items?.find(row=>row.line_id===lineId)||{};const label=document.createElement('label');label.className='inline-choice group-member';const check=document.createElement('input');check.type='checkbox';check.dataset.groupMember=group.group_id;check.value=lineId;label.append(check,document.createTextNode([line.product_name,line.specification,line.material,line.grade,line.surface_color,(line.quantity??'')+' '+(line.unit||'')].filter(Boolean).join(' · ')));details.append(label);}
      block.append(details);if(group.category_stale)addText(block,'p','此类别已有新版本，请在“调整分类与保存常用类别”中选择最新版本并应用。','hint');for(const warning of group.warnings||[])addText(block,'p',typeof warning==='string'?warning:warning.message||JSON.stringify(warning),'hint');parent.append(block);
    }
    if(groups.some(group=>group.group_id===current))el('document-group-selected').value=current;else el('document-group-selected').value=groups[0]?.group_id||'';
    el('document-groups-adjust').hidden=!groups.length;categoryEditor();
  }
  function groupsChanged(message){model.groupsDirty=true;invalidate(message);renderGroups();renderHsLines();el('document-fields').replaceChildren();el('document-supplements').hidden=true;say(message);}
  function moveMembers(){
    if(!requireGroups())return;const selected=[...el('document-groups-list').querySelectorAll('[data-group-member]:checked')];if(!selected.length){say('请先展开类别成员并勾选需要调整的产品。',true);return;}
    const ids=new Set(selected.map(node=>node.value)),targetId=el('document-group-target').value;let target=groupFor(targetId);
    const affected=model.customsGroups.filter(group=>group.member_line_ids.some(id=>ids.has(id)));
    if(targetId&&!target){say('请选择有效的目标类别。',true);return;}
    if(target&&affected.every(group=>group.group_id===targetId)){say('勾选产品已经在目标类别中。');return;}
    if(!target&&affected.length!==1){say('拆分类别时，请只勾选同一个类别中的产品。',true);return;}
    if(!target&&affected[0].member_line_ids.length===ids.size){say('请保留当前类别至少一个成员；全部成员无需拆分。',true);return;}
    if(!target){target={...structuredClone(affected[0]),group_id:'group_manual_'+Date.now().toString(36)+'_'+Math.random().toString(36).slice(2,7),name:groupTitle(affected[0])+'（拆分）',member_line_ids:[],members:[],category_id:null,fields:{...(affected[0].fields||{})},catalog_snapshot_id:null};delete target.fields.legal_quantity;model.customsGroups.push(target);}
    const previousMembers=[...(target.members||[])];for(const group of affected){for(const member of group.members||[])if(ids.has(member.line_id))previousMembers.push(member);group.member_line_ids=group.member_line_ids.filter(id=>!ids.has(id));delete group.fields?.legal_quantity;}
    target.member_line_ids=[...new Set([...target.member_line_ids,...ids])];target.members=previousMembers.filter((row,index,all)=>target.member_line_ids.includes(row.line_id)&&all.findIndex(member=>member.line_id===row.line_id)===index);delete target.fields?.legal_quantity;
    model.customsGroups=model.customsGroups.filter(group=>group.member_line_ids.length);groupsChanged('分类成员已调整，原类别净重已清除。请重新预览，核对分类适用范围并补充各类净重。');
  }
  async function loadCategories(selected=el('document-category-select').value){const result=await api('/customs/categories');model.categories=Array.isArray(result)?result:result.categories||[];const select=el('document-category-select');select.replaceChildren();const empty=document.createElement('option');empty.value='';empty.textContent='请选择常用类别';select.append(empty);for(const category of model.categories){const option=document.createElement('option');option.value=category.id;option.textContent=category.name+' · '+category.hs_code+' · v'+category.version;select.append(option);}select.value=model.categories.some(row=>row.id===selected)?selected:'';}
  function applyCategory(){
    const group=selectedGroup(),category=model.categories.find(row=>row.id===el('document-category-select').value);if(!group||!category){say('请选择当前类别和一个已保存类别。',true);return;}
    group.category_id=category.id;group.name=category.name;group.fields??={};for(const key of ['hs_code','description','declaration_elements_text','origin_country','destination_country','domestic_origin','levy_method','legal_unit'])if(typeof category[key]==='string'&&category[key].trim())group.fields[key]=category[key];group.catalog_snapshot_id=category.catalog_snapshot_id||null;
    groupsChanged('已应用常用类别。请重新预览核对成员是否符合分类条件，并填写本次申报资料。');
  }
  async function saveCategory(){
    const group=selectedGroup();if(!group)throw new Error('请先预览并选择当前类别。');const fields=group.fields||{},name=el('document-category-name').value.trim(),hs=el('document-category-hs').value.trim(),reason=el('document-category-reason').value.trim();if(!name||!hs||!reason)throw new Error('请填写类别名称、HS 编码和本次保存原因。');
    const existing=model.categories.find(row=>row.name===name);const data={name,hs_code:hs,product_name:group.product_name||'',material:group.material||'',grade:group.grade||'',diameter_min:el('document-category-min').value.trim()||null,diameter_max:el('document-category-max').value.trim()||null,min_inclusive:el('document-category-min-inclusive').checked,max_inclusive:el('document-category-max-inclusive').checked,description:el('document-category-description').value,declaration_elements_text:el('document-category-elements').value,change_reason:reason,expected_version:existing?.version||0,catalog_snapshot_id:hs===fields.hs_code?(group.catalog_snapshot_id||null):null};
    for(const key of ['origin_country','destination_country','domestic_origin','levy_method','legal_unit'])if(fields[key]!=null)data[key]=fields[key];
    const saved=await api('/customs/categories',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});await loadCategories(saved.id);group.category_id=saved.id;group.name=saved.name;group.fields={...fields};for(const key of ['hs_code','description','declaration_elements_text'])if(typeof saved[key]==='string'&&saved[key].trim())group.fields[key]=saved[key];group.catalog_snapshot_id=saved.catalog_snapshot_id||null;groupsChanged('常用类别已保存并应用到当前分类。请重新预览核对，后续导入会按保存条件匹配。');
  }
  function hsChoice(format){el('document-hs-code').value=format?.hs_code||'';el('document-hs-name').value=format?.name||'';el('document-hs-description').value=format?.description_template||'';el('document-hs-elements').value=format?.declaration_elements_template||'';el('document-hs-reason').value='';el('document-hs-version').textContent=format?'已保存版本 '+format.version:'新格式，保存后可按 HS 编码复用。';}
  async function loadHsFormats(selected=el('document-hs-format').value){const rows=await api('/customs/hs-formats');model.hsFormats=Array.isArray(rows)?rows:rows.formats||[];const select=el('document-hs-format');select.replaceChildren();const fresh=document.createElement('option');fresh.value='';fresh.textContent='新建用户申报格式';select.append(fresh);for(const row of model.hsFormats){const option=document.createElement('option');option.value=row.id;option.textContent=row.hs_code+' · '+row.name+' · v'+row.version;select.append(option);}select.value=model.hsFormats.some(row=>row.id===selected)?selected:'';}
  function customsVisibility(){el('document-customs-tools').hidden=!document.querySelector('[name=document-type][value=customs]').checked;renderHsLines();}
  function showPreview(result){
    model.preview=result;const conversion=result.conversion||{},totals=conversion.totals||{};
    if(Array.isArray(result.customs_groups)){model.customsGroups=structuredClone(result.customs_groups);model.groupsDirty=false;renderGroups();renderHsLines();}
    el('document-preview').hidden=false;
    el('document-conversion-summary').textContent='本次汇率：1 USD = '+conversion.exchange_rate+' CNY · 人民币合计 '+(totals.total_cny??'—')+' · 美元合计 '+(totals.total_usd??'—')+' USD';
    el('document-rounding').textContent='美元单价 4 位，行金额 2 位，总额汇总行金额。与人民币总额直接换汇的差额：'+(totals.rounding_difference_usd??'未提供')+' USD。';
    makeTable(el('document-conversion-table'),[['品名','product_name',true],['规格','specification',true],['数量','quantity'],['人民币单价','unit_price_cny'],['人民币金额','amount_cny'],['美元单价','unit_price_usd'],['美元金额','amount_usd']],conversion.items||[],'本次换算预览；金额由服务端按十进制精确计算。');
    makeTable(el('document-readiness-table'),[['单据',row=>row.label||labels[row.document_type]],['默认模板','template_filename',true],['状态',row=>row.ready?'可生成':row.missing?.length?'待补充':row.unconfirmed_count?'待核对 '+row.unconfirmed_count+' 项':'待补充'],['需要补充',row=>missingText(row.missing)||row.note||'—',true]],result.documents||[],'自动使用已适配的默认模板。');
    const warnings=el('document-warnings');warnings.replaceChildren();for(const warning of result.warnings||[])addText(warnings,'li',typeof warning==='string'?warning:warning.message||JSON.stringify(warning));
    renderFields(result.field_specs||[]);
    renderCustomsReview(result.customs_review);
    renderCatalogReview(result.hs_catalog_review||[]);
    say(result.ready?'预览已完成，可以生成所选单据。生成后请核对版式和业务信息。':result.customs_review?.items?.some(item=>!item.confirmed)?'请补齐缺项，并逐项核对报关内容后重新预览。':'换算预览已完成，请补充下方所选单据需要的信息，再重新预览。');
    controls();
  }
  function downloadLink(parent,url,label){if(typeof url!=='string'||!url.startsWith('/')||url.startsWith('//'))return;const link=document.createElement('a');link.href=url;link.textContent=label;link.className='download-link';parent.append(link);}
  function showJob(record,parent){const payload=record.payload||{};const block=document.createElement('article');block.className='document-result';addText(block,'h3',payload.source_order_no||'已生成单据');addText(block,'p','汇率：1 USD = '+payload.exchange_rate+' CNY · '+((payload.document_types||[]).map(type=>labels[type]||type).join('、')),'hint');const links=document.createElement('div');links.className='actions';downloadLink(links,payload.zip_download_url,'整套 ZIP 下载');for(const file of payload.files||[])downloadLink(links,file.download_url,file.filename||labels[file.document_type]||'下载单据');block.append(links);addText(block,'p','请预览核对后使用。签收人、签收时间和签字由实际签收环节填写。','hint');parent.append(block);}
  async function loadJobs(){const result=await api('/document-jobs');const rows=Array.isArray(result)?result:result.records||[];const parent=el('document-history');parent.replaceChildren();if(!rows.length)addText(parent,'p','暂无制单记录。','empty');for(const row of rows)showJob(row,parent);}
  el('document-import').addEventListener('submit',event=>{event.preventDefault();task(el('document-import-button'),async()=>{const file=el('document-file').files[0];if(!file)throw new Error('请选择 ERP Excel 文件。');say('正在读取 ERP 产品和人民币价格…');const form=new FormData();form.append('file',file);const result=await api('/document-jobs/import',{method:'POST',body:form});await loadSources(result.source_id);resetSource();say('ERP 已导入，请核对产品和人民币价格，并填写本次汇率。');});});
  el('document-source').addEventListener('change',resetSource);
  el('document-new').addEventListener('click',()=>{resetSource();el('document-downloads').replaceChildren();el('document-downloads-title').hidden=true;say('已开始新的制单，请手动填写并确认本次汇率。');el('document-rate').focus();});
  el('document-rate').addEventListener('input',()=>{invalidate();if(model.customsGroups?.length){model.groupsDirty=true;renderGroups();}});
  for(const checkbox of document.querySelectorAll('[name=document-type]'))checkbox.addEventListener('change',()=>{invalidate();el('document-supplements').hidden=true;customsVisibility();});
  el('document-refresh-sources').addEventListener('click',event=>task(event.currentTarget,async()=>{await loadSources();invalidate();say('ERP 来源列表已刷新，请重新预览当前制单内容。');}));
  el('document-prepare').addEventListener('submit',event=>{event.preventDefault();task(el('document-preview-button'),async()=>{const body=requestBody();if(!body.source_id)throw new Error('请选择或上传 ERP Excel。');if(!body.exchange_rate||!/^\d+(?:\.\d+)?$/.test(body.exchange_rate)||!/[1-9]/.test(body.exchange_rate))throw new Error('请填写大于零的本次汇率，口径为 1 美元等于多少人民币。');if(!body.document_types.length)throw new Error('请至少选择一种单据。');invalidate('正在重新检查换算与所选单据…',true);say('正在计算换算并检查单据缺项…');const result=await api('/document-jobs/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});showPreview(result);});});
  el('document-hs-format').addEventListener('change',event=>hsChoice(model.hsFormats.find(row=>row.id===event.target.value)));
  el('document-customs-confirm-all').addEventListener('click',()=>{
    if(!model.review?.items?.length)return;
    for(const item of model.review.items)if(item.token)model.customs_confirmations[item.id]=item.token;
    invalidate('已记录你的全部核对确认，请重新预览后生成。',true);renderCustomsReview(model.review);
    say('已记录你的全部核对确认；缺失项目仍需补齐。请重新预览后生成。');
  });
  el('document-hs-refresh').addEventListener('click',event=>task(event.currentTarget,async()=>{await loadHsFormats();say('已刷新用户保存的 HS 申报格式。');}));
  el('document-hs-save').addEventListener('click',event=>task(event.currentTarget,async()=>{
    const hsCode=el('document-hs-code').value.trim();const existing=model.hsFormats.find(row=>row.hs_code===hsCode);const data={hs_code:hsCode,name:el('document-hs-name').value.trim(),description_template:el('document-hs-description').value,declaration_elements_template:el('document-hs-elements').value,change_reason:el('document-hs-reason').value.trim(),expected_version:existing?.version||0};
    if(!data.hs_code||!data.name||!data.description_template.trim()||!data.change_reason)throw new Error('请填写 HS 编码、格式名称、商品名称规格格式及本次保存原因。');
    const saved=await api('/customs/hs-formats',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});await loadHsFormats(saved.id);hsChoice(saved);invalidate();say('用户申报格式已保存。请应用到需要的申报类别，再重新预览并核对。');
  }));
  el('document-hs-apply').addEventListener('click',()=>{
    if(!requireGroups())return;
    const selected=model.hsFormats.find(row=>row.id===el('document-hs-format').value);if(!selected){say('请先保存当前格式，或选择一个已保存格式。',true);return;}
    if(selected.hs_code!==el('document-hs-code').value.trim()||selected.name!==el('document-hs-name').value.trim()||selected.description_template!==el('document-hs-description').value||selected.declaration_elements_template!==el('document-hs-elements').value){say('格式内容有未保存的修改，请先保存再应用。',true);return;}
    const rows=customsTargets(el('document-hs-line').value);if(!rows.length){say('请先选择 ERP 商品来源。',true);return;}
    for(const row of rows){if(row.group_id){row.fields??={};row.fields.hs_code=selected.hs_code;row.catalog_snapshot_id=null;row.category_id=null;}else{model.item_extras[row.line_id]??={};model.item_extras[row.line_id].hs_code=selected.hs_code;delete model.hs_catalog_bindings[row.line_id];}}
    invalidate();renderGroups();say('已为 '+rows.length+' 个申报类别设置 HS 编码 '+selected.hs_code+'。已有手填商品名称规格保留；请重新预览并核对。');
  });
  function catalogSay(message,error=false){const node=el('document-hs-catalog-message');node.textContent=message;node.className='hs-catalog-note '+(error?'status error':'hint');}
  function catalogTask(button,work){return task(button,async()=>{try{await work();}catch(error){catalogSay(error.message,true);throw error;}});}
  function catalogState(value){return value==='active'?'正常':value==='expired'?'已失效':'状态未知，需核实';}
  function catalogTime(value){if(!value)return '未提供';const date=new Date(value);return Number.isNaN(date.getTime())?String(value):date.toLocaleString('zh-CN',{hour12:false});}
  async function loadCatalogStatus(){
    if(model.catalogStatusLoading)return;
    model.catalogStatusLoading=true;const button=el('document-hs-catalog-status-refresh');button.textContent='检查中…';button.setAttribute('aria-busy','true');
    el('document-hs-catalog-status').textContent='正在检查本机配置是否存在；本次检查不请求服务商，不消耗服务商查询额度。';controls();
    try{model.catalogStatus=await api('/customs/hs-catalog/status');const status=model.catalogStatus;
      el('document-hs-catalog-status').textContent=catalogProviderLabel(status.provider,status.protocol)+' · '+(status.configured?'已配置，尚未验证服务可用性':'尚未配置')+' · '+(status.https_only?'仅使用 HTTPS。':'')+'本次仅检查本机配置是否存在，不请求服务商、不消耗服务商查询额度。'+(status.configured?'请实际查询编码以验证服务。':'仍可手动填写 HS 编码或使用下方已保存的申报格式。请按项目 HS 接口配置说明在本机环境变量中配置密钥，再重启服务。')+(status.note?' '+status.note:'');
    }catch(error){model.catalogStatus=null;el('document-hs-catalog-status').textContent='本机配置状态读取失败，仍可手动填写 HS 或使用已保存格式。'+error.message;}
    finally{model.catalogStatusLoading=false;button.textContent='检查本机配置';button.removeAttribute('aria-busy');controls();}
  }
  function markCatalogQueryResult(result,liveSearch=false){
    const provider=result?.provider||model.catalogStatus?.provider;const protocol=result?.protocol||(!result?.provider?model.catalogStatus?.protocol:undefined);
    const message=result?.cache_hit===true?'已读取目录缓存；缓存不代表本次鉴权':result?.cache_hit===false||liveSearch?'本次服务查询成功':'已读取目录信息；本次服务鉴权状态未提供';
    el('document-hs-catalog-status').textContent=catalogProviderLabel(provider,protocol)+' · '+message+'。请继续按实际货物核对编码及申报要素。';
  }
  function renderCatalogDetail(detail){
    model.catalogDetail=detail;const parent=el('document-hs-catalog-detail-content');parent.replaceChildren();el('document-hs-catalog-detail').hidden=false;el('document-hs-catalog-code').value=detail.hs_code||'';
    addText(parent,'p',(detail.hs_code||'未提供编码')+' · '+(detail.name||'未提供名称'));
    if(detail.remark)addText(parent,'p',detail.remark,'hs-catalog-note');
    addText(parent,'p','编码状态：'+catalogState(detail.status)+(detail.stale?' · 缓存已超过刷新期限，请重新向服务商查询。':''),'hint');
    addText(parent,'p','法定单位：'+([detail.unit1,detail.unit2].filter(Boolean).join(' / ')||'未提供'),'hint');
    addText(parent,'p','来源：'+catalogProviderLabel(detail.provider,detail.protocol)+' · 本地版本：'+(detail.version??'未提供')+' · '+(detail.cache_hit?'本地缓存':'本次查询'),'hint');
    addText(parent,'p','服务商更新于：'+catalogTime(detail.provider_updated_at)+'；查询于：'+catalogTime(detail.fetched_at),'hint');
    if(detail.content_hash)addText(parent,'p','内容校验：'+detail.content_hash,'hint');
    const list=el('document-hs-catalog-elements');list.replaceChildren();
    for(const item of [...(detail.reporting_elements||[])].sort((a,b)=>Number(a.order)-Number(b.order)))addText(list,'li',(item.order==null?'':item.order+'：')+(item.name||'未提供要素名称')+'（'+(item.required===true?'必填':item.required===false?'可选':'是否必填待核实')+'）');
    if(!list.childElementCount)addText(list,'li','服务商未提供申报要素；请核实后手动填写。');
    el('document-hs-catalog-apply').textContent=detail.status==='expired'?'此编码已失效，不能应用':detail.stale?'请先刷新目录后再应用':detail.status==='active'?'将此 HS 编码用于所选类别':'核实后将此 HS 编码用于所选类别';controls();
  }
  async function loadCatalogDetail(code,refresh=false){
    code=code.trim();if(!/^(?:\d{10}|\d{8}\.\d{2})$/.test(code))throw new Error('请填写 10 位 HS 编码，也可使用 8 位加小数点加 2 位的格式。');
    catalogSay(refresh?'正在向服务商重新查询编码详情…':'正在读取编码详情…');
    model.catalogDetail=null;el('document-hs-catalog-detail').hidden=true;
    const detail=await api('/customs/hs-catalog/codes/'+encodeURIComponent(code)+'?refresh='+String(refresh));renderCatalogDetail(detail);markCatalogQueryResult(detail);
    const changedBinding=(model.customsGroups||[]).some(group=>group.catalog_snapshot_id&&group.fields?.hs_code===detail.hs_code&&(group.catalog_snapshot_id!==detail.snapshot_id||detail.status==='expired'||detail.stale===true))||Object.entries(model.hs_catalog_bindings).some(([line,id])=>model.item_extras[line]?.hs_code===detail.hs_code&&(id!==detail.snapshot_id||detail.status==='expired'||detail.stale===true));
    if(changedBinding)invalidate('已应用编码的目录信息发生变化，请重新应用最新目录并预览核对。');
    catalogSay(detail.status==='expired'?'此编码已失效，请查询适用的现行编码。':detail.status==='unknown'?'服务商未确认此编码状态，请核实后再应用。':'已读取编码详情。应用前请核对分类范围和申报要素。',detail.status==='expired');
  }
  async function searchCatalog(page=1){
    const keywords=page===1?el('document-hs-catalog-keywords').value.trim():model.catalogKeywords;if(!keywords)throw new Error('请先填写要查询的品名或编码。');
    catalogSay('正在查询 HS 目录…');const result=await api('/customs/hs-catalog/search?keywords='+encodeURIComponent(keywords)+'&page='+page);model.catalogKeywords=keywords;model.catalogPage=page;markCatalogQueryResult(result,true);
    const rows=Array.isArray(result)?result:result.items||[];const parent=el('document-hs-catalog-results');parent.replaceChildren();
    for(const row of rows){const button=document.createElement('button');button.type='button';button.className='secondary hs-catalog-result';button.textContent=[row.hs_code,row.name,row.remark,row.units,'状态：'+catalogState(row.status)].filter(Boolean).join(' · ');button.addEventListener('click',()=>catalogTask(button,()=>loadCatalogDetail(row.hs_code)));parent.append(button);}
    el('document-hs-catalog-next').hidden=!rows.length||result.has_more===false;
    catalogSay(rows.length?'第 '+page+' 页，共 '+rows.length+' 个候选。点击候选查看完整详情，再确认适用范围。':'本页没有查询结果。可调整关键词，或直接输入完整 HS 编码查看详情。');
  }
  function renderCatalogReview(rows){
    const parent=el('document-hs-catalog-review');parent.replaceChildren();parent.hidden=!rows.length;
    for(const row of rows){const line=groupFor(row.line_id)||source()?.items?.find(item=>item.line_id===row.line_id);let text=(line?.name||line?.specification||row.line_id||'申报类别')+' · HS '+row.hs_code+' · '+catalogState(row.status);
      if(row.unit_unsupported)text+='；当前模板仅适配第一法定单位为千克，此编码单位需要调整模板后使用。';if(row.blocked)text+='；此目录引用当前阻止生成。';
      if(row.changed)text+='；目录已有新版本，请查看最新要求并重新应用后核对。';if(row.stale)text+='；目录缓存超过刷新期限。';
      if(row.provider_changed)text+='；接口类型已切换，请在当前接口重新查询并应用。';
      addText(parent,'p',text);}
  }
  el('document-hs-catalog-status-refresh').addEventListener('click',()=>loadCatalogStatus());
  el('document-hs-catalog-search').addEventListener('click',event=>catalogTask(event.currentTarget,()=>searchCatalog(1)));
  el('document-hs-catalog-next').addEventListener('click',event=>catalogTask(event.currentTarget,()=>searchCatalog(model.catalogPage+1)));
  el('document-hs-catalog-detail-button').addEventListener('click',event=>catalogTask(event.currentTarget,async()=>{try{await loadCatalogDetail(el('document-hs-catalog-code').value);}catch(error){catalogSay(error.message,true);throw error;}}));
  el('document-hs-catalog-refresh').addEventListener('click',event=>catalogTask(event.currentTarget,async()=>{try{await loadCatalogDetail(el('document-hs-catalog-code').value,true);}catch(error){catalogSay(error.message,true);throw error;}}));
  el('document-hs-catalog-apply').addEventListener('click',()=>{
    if(!requireGroups())return;
    const detail=model.catalogDetail;if(!detail?.snapshot_id||detail.status==='expired'||detail.stale===true){catalogSay('请先查看有效且已刷新的编码详情，失效或过期缓存不能应用。',true);return;}
    const rows=customsTargets(el('document-hs-catalog-line').value);if(!rows.length){catalogSay('请先选择 ERP 商品来源。',true);return;}
    for(const row of rows){if(row.group_id){row.fields??={};row.fields.hs_code=detail.hs_code;row.catalog_snapshot_id=detail.snapshot_id;row.category_id=null;}else{model.item_extras[row.line_id]??={};model.item_extras[row.line_id].hs_code=detail.hs_code;model.hs_catalog_bindings[row.line_id]=detail.snapshot_id;}}
    invalidate();renderGroups();el('document-hs-catalog-review').hidden=true;const message='已为 '+rows.length+' 个申报类别应用 HS '+detail.hs_code+' 并记录目录版本。请重新预览，填写并核对本次货物的申报内容。';catalogSay(message);say(message);
  });
  el('document-hs-catalog-unbind').addEventListener('click',()=>{
    if(!requireGroups())return;const rows=customsTargets(el('document-hs-catalog-line').value);let count=0;
    for(const row of rows)if(row.group_id?row.catalog_snapshot_id:own(model.hs_catalog_bindings,row.line_id)){if(row.group_id)row.catalog_snapshot_id=null;else delete model.hs_catalog_bindings[row.line_id];count++;}
    if(!count){catalogSay('所选类别没有目录引用，现有 HS 编码和手填内容保留。');return;}
    invalidate();el('document-hs-catalog-review').hidden=true;const message='已取消 '+count+' 个申报类别的目录引用，保留 HS 编码和已填内容。请手动核实本次申报要求，并重新预览核对。';catalogSay(message);say(message);
  });
  el('document-hs-catalog-updates').addEventListener('click',event=>task(event.currentTarget,async()=>{
    if(!catalogCapability('updates')){renderCatalogCapabilities();return;}
    const summary=el('document-hs-catalog-updates-summary');summary.textContent='正在查询更新记录…';const parent=el('document-hs-catalog-updates-list');parent.replaceChildren();
    try{const result=await api('/customs/hs-catalog/updates?range='+encodeURIComponent(el('document-hs-catalog-range').value));const rows=result.items||[];
      const changedCodes=new Set(rows.map(row=>String(row.hs_code||row.hsCode||'').replace(/\./g,'')));
      const affectedBinding=(model.customsGroups||[]).some(group=>group.catalog_snapshot_id&&changedCodes.has(String(group.fields?.hs_code||'').replace(/\./g,'')))||Object.keys(model.hs_catalog_bindings).some(line=>changedCodes.has(String(model.item_extras[line]?.hs_code||'').replace(/\./g,'')));
      if(affectedBinding){
        invalidate('更新记录涉及已应用的 HS 编码，请重新查询详情、应用最新目录后再预览核对。');
        if(model.catalogDetail&&changedCodes.has(String(model.catalogDetail.hs_code||'').replace(/\./g,'')))renderCatalogDetail({...model.catalogDetail,stale:true});
        const message='更新记录涉及本次已引用的 HS 编码，旧预览和确认已失效。请重新查询对应编码详情，应用最新目录后再预览核对。';catalogSay(message);say(message);
      }
      summary.textContent='本次返回 '+rows.length+' 条'+(result.total_count!=null?'，服务商记录总数 '+result.total_count+' 条':'')+'；查询于 '+catalogTime(result.fetched_at)+'。更新范围与及时性以服务商返回为准。';
      if(!rows.length)addText(parent,'p','所选范围内未返回更新记录。','hint');
      for(const row of rows){const block=document.createElement('div');block.className='hs-catalog-update';addText(block,'strong',[row.hs_code||row.hsCode,row.field,row.change_type||row.changeType].filter(Boolean).join(' · '));addText(block,'p','更新于：'+catalogTime(row.changed_at||row.create_time||row.createTime||row.updated_at),'hint');if(row.before!=null)addText(block,'p','修改前：'+(typeof row.before==='string'?row.before:JSON.stringify(row.before)));if(row.after!=null)addText(block,'p','修改后：'+(typeof row.after==='string'?row.after:JSON.stringify(row.after)));parent.append(block);}
    }catch(error){summary.textContent='更新记录查询失败：'+error.message;throw error;}
  }));
  el('document-generate').addEventListener('click',event=>task(event.currentTarget,async()=>{if(!model.preview?.ready||!model.preview.preview_key)throw new Error('请先完成当前内容的换算预览。');const body={...requestBody(),preview_key:model.preview.preview_key};say('正在按默认模板生成所选单据…');const result=await api('/document-jobs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const parent=el('document-downloads');parent.replaceChildren();showJob(result,parent);el('document-downloads-title').hidden=false;await loadJobs();say('所选单据已生成，可下载单份文件或整套 ZIP。请核对后使用。');parent.focus();}));
  el('document-group-selected').addEventListener('change',categoryEditor);
  el('document-group-move').addEventListener('click',moveMembers);
  el('document-category-apply').addEventListener('click',applyCategory);
  el('document-category-save').addEventListener('click',event=>task(event.currentTarget,saveCategory));
  let loaded=false;
  async function activate(){
    if(location.hash!=='#document-jobs'||loaded)return;loaded=true;
    const results=await Promise.allSettled([loadSources(),loadJobs(),loadHsFormats(),loadCatalogStatus(),loadCategories()]);
    customsVisibility();
    const failures=results.filter(result=>result.status==='rejected');
    if(failures.length){loaded=false;say(failures.map(result=>result.reason?.message||'数据读取失败，请重试。').join('；'),true);}
  }
  window.addEventListener('hashchange',activate);activate();customsVisibility();controls();
})();
