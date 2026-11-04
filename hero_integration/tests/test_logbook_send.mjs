import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const source=readFileSync(new URL('../public/js/hero_logbook_send.js',import.meta.url),'utf8');
class Element {
    constructor(tag){this.tag=tag;this.children=[];this.attributes={};this.style={};this.value='';this.disabled=false;}
    set textContent(value){this.value=String(value);this.children=[];}
    get textContent(){return this.value+this.children.map(child=>typeof child==='string'?child:child.textContent).join('');}
    append(...children){this.children.push(...children);}
    setAttribute(name,value){this.attributes[name]=value;}
}
class Parser {
    parseFromString(value){
        const plain=value.replace(/<(script|style|template)\b[^>]*>[\s\S]*?<\/\1>/gi,'').replace(/<br\s*\/?\s*>/gi,'\n')
            .replace(/<\/(?:p|div|li)>/gi,'\n').replace(/<[^>]*>/g,'').replaceAll('&lt;','<').replaceAll('&gt;','>').replaceAll('&amp;','&');
        return {body:{textContent:plain},querySelectorAll:()=>[]};
    }
}
const comments=[{name:'COMMENT-1',content:'<p>Installed &amp; tested.</p><script>steal()</script>',creation:'2026-11-04 10:00:00'},
    {name:'COMMENT-2',content:'<p>Second activity.</p>',creation:'2026-11-04 11:00:00'}];
const preview=comment=>({comment,project:'PROJECT-1',changes:[{label:'Activity text',hero:null,new:'<p>Mapped text</p>'}],
    can_send:true,sending_enabled:true,fingerprint:`fp-${comment}`});
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
async function setup(overrides={},options={}){
    const calls=[],dialogs=[],messages=[],alerts=[],buttons=new Map();let controller,dirty=false,reloads=0;
    let route=['Form','Project','PROJECT-1'];
    const methods={project_logbook_candidates:()=>({comments}),project_logbook_preview:({comment})=>preview(comment),
        project_logbook_send:()=>({status:'SENT'}),...overrides};
    class Dialog {
        constructor(config){this.config=config;this.shown=false;this.button={disabled:false};this.fields_dict={};
            for(const df of config.fields){
                const element=new Element('div');const wrapper={0:element,empty(){element.children=[];element.value='';return this;}};
                const input={disabled:false,prop(key,value){this[key]=value;return this;}};
                this.fields_dict[df.fieldname]={df,value:'',$wrapper:wrapper,$input:input,refresh(){},get_value(){return this.value;},
                    set_value(value){this.value=value;return df.onchange?.();}};
            }
            dialogs.push(this);
        }
        get_primary_btn(){const button=this.button;return {prop(key,value){button[key]=value;}};}
        show(){this.shown=true;}
        hide(){this.shown=false;this.config.on_hide?.();}
    }
    const frappe={user:{has_role:role=>(options.roles||['System Manager']).includes(role)},ui:{Dialog,form:{on:(doctype,events)=>{assert.equal(doctype,'Project');controller=events;}}},
        get_route:()=>route,msgprint:message=>messages.push(message),show_alert:alert=>alerts.push(alert),
        call:async request=>{const key=request.method.split('.').at(-1);calls.push({...request,key});assert.ok(methods[key],key);return {message:await methods[key](request.args)};}};
    vm.runInNewContext(source,{frappe,__:value=>value,document:{createElement:tag=>new Element(tag)},DOMParser:Parser});
    const frm={doc:{name:'PROJECT-1',modified:'2026-11-04 12:00:00'},is_new:()=>options.isNew||false,is_dirty:()=>dirty,
        add_custom_button:(label,action)=>buttons.set(label,action),remove_custom_button:label=>buttons.delete(label),reload_doc:async()=>{reloads++;}};
    const flush=async()=>{for(let index=0;index<12;index++)await Promise.resolve();};
    controller.refresh(frm);await flush();
    const open=async()=>{buttons.get('Send activity to HERO')?.();await flush();return dialogs.at(-1);};
    const choose=async(name='COMMENT-1',kind='Comment')=>{const pending=dialogs.at(-1).fields_dict.activity.set_value(JSON.stringify([kind,name]));await flush();return pending;};
    return {frm,controller,calls,dialogs,messages,alerts,buttons,flush,open,choose,setDirty:value=>{dirty=value;},setRoute:value=>{route=value;},reloads:()=>reloads};
}

test('saved Project gets one activity action only for authorized roles, without automatic RPC',async()=>{
    for(const roles of [['System Manager'],['HERO Writeback'],['Employee']]){
        const app=await setup({}, {roles});
        assert.equal(app.buttons.size,roles[0]==='Employee'?0:1);
        assert.equal(app.calls.length,0);
        app.controller.refresh(app.frm);assert.equal(app.buttons.size,roles[0]==='Employee'?0:1);
    }
    assert.equal((await setup({}, {isNew:true})).buttons.size,0);
    const hooks=readFileSync(new URL('../hooks.py',import.meta.url),'utf8');
    assert.match(hooks,/"Project": \[[^\n]+"public\/js\/hero_logbook_send\.js"/);
});

test('dirty Projects block before candidates are requested',async()=>{
    const app=await setup();app.setDirty(true);await app.open();
    assert.equal(app.calls.length,0);assert.match(app.messages[0],/Save the project/);assert.equal(app.dialogs.length,0);
});

test('activity selection previews safe plain text and sends exactly once after explicit confirmation',async()=>{
    const app=await setup();const dialog=await app.open();
    assert.equal(app.calls[0].key,'project_logbook_candidates');assert.equal(app.calls[0].type,'GET');
    assert.equal(dialog.button.disabled,true);
    const label=dialog.fields_dict.activity.df.options[1].label;
    assert.match(label,/Installed & tested/);assert.doesNotMatch(label,/<p>|script|steal/);
    await app.choose();
    assert.equal(dialog.button.disabled,false);
    assert.equal(app.calls.some(call=>call.key==='project_logbook_send'),false);
    const shown=dialog.fields_dict.summary.$wrapper[0].textContent;
    assert.match(shown,/Installed & tested/);assert.match(shown,/Mapped text/);assert.doesNotMatch(shown,/<p>|script/);
    await dialog.config.primary_action();await dialog.config.primary_action();
    const sends=app.calls.filter(call=>call.key==='project_logbook_send');assert.equal(sends.length,1);
    assert.equal(sends[0].type,'POST');
    assert.deepEqual(JSON.parse(JSON.stringify(sends[0].args)),{comment:'COMMENT-1',activity_doctype:'Comment',fingerprint:'fp-COMMENT-1',confirmed:1});
    assert.equal(app.alerts[0].message,'Activity sent to HERO.');assert.equal(app.reloads(),1);
});

test('candidate list blocked or empty leaves sending unavailable',async()=>{
    for(const result of [{comments:[]},{comments,blocked_reason:'No unique project binding'}]){
        const app=await setup({project_logbook_candidates:()=>result});const dialog=await app.open();
        assert.equal(dialog.button.disabled,true);assert.equal(dialog.fields_dict.activity.$input.disabled,true);
        assert.match(dialog.fields_dict.summary.$wrapper[0].textContent,/No eligible|No unique/);
        await dialog.config.primary_action();assert.equal(app.calls.length,1);
    }
});

test('global sending gate and preview blockers cannot be bypassed by invoking the callback',async()=>{
    for(const flags of [{sending_enabled:false},{can_send:false,blocked_reason:'Mapping needs approval'}]){
        const app=await setup({project_logbook_preview:({comment})=>({...preview(comment),...flags})});
        const dialog=await app.open();await app.choose();
        assert.equal(dialog.button.disabled,true);await dialog.config.primary_action();
        assert.equal(app.calls.some(call=>call.key==='project_logbook_send'),false);
    }
});

test('late preview from a previous selection cannot replace the selected activity fingerprint',async()=>{
    const first=deferred();const app=await setup({project_logbook_preview:({comment})=>comment==='COMMENT-1'?first.promise:preview(comment)});
    const dialog=await app.open();dialog.fields_dict.activity.set_value(JSON.stringify(['Comment','COMMENT-1']));await app.flush();
    await app.choose('COMMENT-2');first.resolve(preview('COMMENT-1'));await app.flush();
    await dialog.config.primary_action();
    assert.equal(app.calls.find(call=>call.key==='project_logbook_send').args.comment,'COMMENT-2');
});

test('cancelled or refreshed forms invalidate pending candidates and previews',async()=>{
    const candidates=deferred();const app=await setup({project_logbook_candidates:()=>candidates.promise});
    const dialog=await app.open();dialog.hide();candidates.resolve({comments});await app.flush();
    assert.equal(dialog.fields_dict.activity.df.options.length,0);
    const next=await setup();const active=await next.open();await next.choose();next.controller.refresh(next.frm);
    await active.config.primary_action();assert.equal(next.calls.some(call=>call.key==='project_logbook_send'),false);
});

test('dirty, changed or navigated Project invalidates an already reviewed preview',async()=>{
    for(const mutate of [app=>app.setDirty(true),app=>{app.frm.doc.modified='newer';},app=>{app.frm.doc.name='OTHER';},app=>app.setRoute(['List','Project'])]){
        const app=await setup();const dialog=await app.open();await app.choose();mutate(app);
        await dialog.config.primary_action();assert.equal(app.calls.some(call=>call.key==='project_logbook_send'),false);
        assert.match(dialog.fields_dict.summary.$wrapper[0].textContent,/project changed/);
    }
});

test('mismatched comment/project responses are never eligible for sending',async()=>{
    for(const changed of [{comment:'OTHER'},{project:'OTHER'},{fingerprint:''}]){
        const app=await setup({project_logbook_preview:({comment})=>({...preview(comment),...changed})});
        const dialog=await app.open();await app.choose();await dialog.config.primary_action();
        assert.equal(dialog.button.disabled,true);assert.match(dialog.fields_dict.summary.$wrapper[0].textContent,/Invalid activity preview/);
        assert.equal(app.calls.some(call=>call.key==='project_logbook_send'),false);
    }
});

test('network ambiguity or unexpected send result cannot retry from the same confirmation',async()=>{
    for(const send of [()=>{throw new Error('Network result unknown');},()=>({status:'BLOCKED'})]){
        const app=await setup({project_logbook_send:send});const dialog=await app.open();await app.choose();
        await dialog.config.primary_action();await dialog.config.primary_action();
        assert.equal(app.calls.filter(call=>call.key==='project_logbook_send').length,1);
        assert.equal(dialog.button.disabled,true);assert.equal(app.alerts.length,0);
        assert.match(dialog.fields_dict.summary.$wrapper[0].textContent,/could not be confirmed as sent/);
    }
});

test('candidate and preview errors remain text and do not enable sending',async()=>{
    for(const endpoint of ['project_logbook_candidates','project_logbook_preview']){
        const app=await setup({[endpoint]:()=>{throw new Error('<img src=x onerror=steal()>Permission denied');}});
        const dialog=await app.open();if(endpoint.endsWith('preview'))await app.choose();
        assert.equal(dialog.button.disabled,true);assert.match(dialog.fields_dict.summary.$wrapper[0].textContent,/Permission denied/);
        assert.doesNotMatch(dialog.fields_dict.summary.$wrapper[0].textContent,/onerror/);
        assert.equal(app.calls.some(call=>call.key==='project_logbook_send'),false);
    }
});

test('long picker labels are shortened while the selected activity body remains complete',async()=>{
    const body='Detailed activity '.repeat(30);
    const app=await setup({project_logbook_candidates:()=>({comments:[{name:'COMMENT-1',content:`<p>${body}</p>`,creation:'2026-11-04 10:00:00'}]})});
    const dialog=await app.open();const label=dialog.fields_dict.activity.df.options[1].label;
    assert.ok(label.length<160);assert.ok(label.endsWith('…'));
    await app.choose();assert.ok(dialog.fields_dict.summary.$wrapper[0].textContent.includes(body.trim()));
});

test('repeated action clicks share one pending activity dialog and never start duplicate requests',async()=>{
    const pending=deferred();const app=await setup({project_logbook_candidates:()=>pending.promise});
    await app.open();await app.open();assert.equal(app.dialogs.length,1);assert.equal(app.calls.length,1);
    pending.resolve({comments});await app.flush();assert.equal(app.calls.length,1);
});


test('Comment and Version sharing a name remain separate choices and send the verified Version kind',async()=>{
    const app=await setup({project_logbook_candidates:()=>({comments:[comments[0],{...comments[0],activity_doctype:'Version',content:'Status: Open → Completed'}]}),
        project_logbook_preview:({comment,activity_doctype})=>({...preview(comment),activity_doctype,fingerprint:`${activity_doctype}-fingerprint`})});
    const dialog=await app.open();const options=dialog.fields_dict.activity.df.options;
    assert.equal(options.length,3);assert.notEqual(options[1].value,options[2].value);
    assert.deepEqual(JSON.parse(options[1].value),['Comment','COMMENT-1']);
    assert.deepEqual(JSON.parse(options[2].value),['Version','COMMENT-1']);
    await app.choose('COMMENT-1','Version');
    const checked=app.calls.find(call=>call.key==='project_logbook_preview');
    assert.deepEqual(JSON.parse(JSON.stringify(checked.args)),{comment:'COMMENT-1',activity_doctype:'Version'});
    assert.match(dialog.fields_dict.summary.$wrapper[0].textContent,/Status: Open → Completed/);
    await dialog.config.primary_action();
    assert.deepEqual(JSON.parse(JSON.stringify(app.calls.find(call=>call.key==='project_logbook_send').args)),
        {comment:'COMMENT-1',activity_doctype:'Version',fingerprint:'Version-fingerprint',confirmed:1});
});

test('unknown activity kinds are rejected before preview or send',async()=>{
    for(const kind of ['Task','Unknown',null,'']){
        const app=await setup({project_logbook_candidates:()=>({comments:[{...comments[0],activity_doctype:kind}]})});
        const dialog=await app.open();await dialog.config.primary_action();
        assert.match(dialog.fields_dict.summary.$wrapper[0].textContent,/Invalid activity list/);
        assert.equal(dialog.button.disabled,true);
        assert.equal(app.calls.some(call=>call.key!=='project_logbook_candidates'),false);
    }
});

test('a preview cannot change an activity kind even when its name and Project match',async()=>{
    for(const responseKind of [undefined,'Comment','Unknown']){
        const app=await setup({project_logbook_candidates:()=>({comments:[{...comments[0],activity_doctype:'Version'}]}),
            project_logbook_preview:({comment})=>({...preview(comment),activity_doctype:responseKind})});
        const dialog=await app.open();await app.choose('COMMENT-1','Version');await dialog.config.primary_action();
        assert.match(dialog.fields_dict.summary.$wrapper[0].textContent,/Invalid activity preview/);
        assert.equal(app.calls.some(call=>call.key==='project_logbook_send'),false);
    }
});
