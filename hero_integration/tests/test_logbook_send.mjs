import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const source=readFileSync(new URL('../public/js/hero_project_send.js',import.meta.url),'utf8');
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
const preview=()=>({project:'PROJECT-1',data_changes:[{label:'Notes',hero:'old',new:'changed'}],activities:[{comment:'C1',activity_doctype:'Comment',changes:[{label:'Activity',new:'Status: Open to Completed'}]}],changes:[{label:'Notes'},{label:'Activity'}],conflicts:[],phases:[],hero:{phase_id:null},can_send:true,sending_enabled:true,fingerprint:'fp-project',delivery:{project:'PROJECT-1',data:'token',activities:[]}});
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
async function setup(overrides={},options={}){
    const calls=[],dialogs=[],messages=[],alerts=[],buttons=new Map();let controller,dirty=false,reloads=0;
    let route=['Form','Project','PROJECT-1'];
    const methods={project_send_preview:()=>preview(),project_send:()=>({status:'SENT'}),...overrides};
    class Dialog {
        constructor(config){this.config=config;this.shown=false;this.button={disabled:false};this.fields_dict={};
            for(const df of config.fields){
                const element=new Element('div');const wrapper={0:element,empty(){element.children=[];element.value='';return this;}};
                const input={disabled:false,prop(key,value){this[key]=value;return this;}};
                this.fields_dict[df.fieldname]={df,value:'',$wrapper:wrapper,$input:df.hidden?undefined:input,refresh(){if(!this.df.hidden)this.$input ||= input;},get_value(){return this.value;},
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
    const open=async()=>{buttons.get('Send to HERO')?.();await flush();return dialogs.at(-1);};
    return {frm,controller,calls,dialogs,messages,alerts,buttons,flush,open,setDirty:value=>{dirty=value;},setRoute:value=>{route=value;},reloads:()=>reloads};
}

test('one action immediately opens loading then displays data and all activities',async()=>{
    const waiting=deferred();const app=await setup({project_send_preview:()=>waiting.promise});
    assert.equal(app.buttons.size,1);assert.equal(app.calls.length,0);
    const dialog=await app.open();assert.equal(dialog.shown,true);assert.equal(dialog.button.disabled,true);
    assert.match(dialog.fields_dict.summary.$wrapper[0].textContent,/Loading Project changes/);
    assert.equal(app.calls[0].type,'POST');
    waiting.resolve(preview());await app.flush();assert.equal(dialog.button.disabled,false);
    const text=dialog.fields_dict.summary.$wrapper[0].textContent;
    assert.match(text,/changed/);assert.match(text,/Status: Open/);assert.match(text,/naming stays/);
    assert.equal(app.calls.length,1);
});
test('confirmation submits complete plan once and reloads after success',async()=>{
    const app=await setup();const dialog=await app.open();
    await dialog.config.primary_action();await dialog.config.primary_action();
    const sends=app.calls.filter(call=>call.key==='project_send');assert.equal(sends.length,1);
    assert.equal(sends[0].type,'POST');assert.equal(sends[0].args.confirmed,1);
    assert.equal(sends[0].args.fingerprint,'fp-project');
    assert.deepEqual(JSON.parse(sends[0].args.delivery),preview().delivery);assert.equal(app.reloads(),1);
});
test('cancel sends nothing and ignores late preview',async()=>{
    const waiting=deferred();const app=await setup({project_send_preview:()=>waiting.promise});
    const dialog=await app.open();dialog.config.secondary_action();
    waiting.resolve(preview());await app.flush();await dialog.config.primary_action();
    assert.equal(app.calls.length,1);assert.equal(app.reloads(),0);assert.equal(dialog.button.disabled,true);
});
test('preview timeout is visible and cannot send',async()=>{
    const app=await setup({project_send_preview:()=>{throw new Error('Request timed out');}});
    const dialog=await app.open();assert.equal(dialog.shown,true);assert.equal(dialog.button.disabled,true);
    assert.match(dialog.fields_dict.summary.$wrapper[0].textContent,/timed out/);
    await dialog.config.primary_action();assert.equal(app.calls.length,1);
});
test('dirty documents lost permissions and stale route prevent sends',async()=>{
    const app=await setup();app.setDirty(true);await app.open();assert.equal(app.calls.length,0);
    app.setDirty(false);const dialog=await app.open();app.setRoute(['Form','Project','OTHER']);
    await dialog.config.primary_action();assert.equal(app.calls.length,1);
    assert.equal((await setup({}, {roles:['Employee']})).buttons.size,0);
    assert.equal((await setup({}, {isNew:true})).buttons.size,0);
});
test('conflicts global disable and no changes disable confirmation',async()=>{
    for(const value of [{...preview(),can_send:false,conflicts:[{label:'Notes'}]},
        {...preview(),can_send:false,sending_enabled:false},
        {...preview(),can_send:false,changes:[],data_changes:[],activities:[]}]){
        const app=await setup({project_send_preview:()=>value});const dialog=await app.open();
        assert.equal(dialog.button.disabled,true);await dialog.config.primary_action();assert.equal(app.calls.length,1);
    }
});
test('partial or unknown result is displayed and never retried',async()=>{
    for(const response of [()=>({status:'STOPPED',changes:['Notes'],activities:[]}),()=>{throw new Error('Lost response');}]){
        const app=await setup({project_send:response});const dialog=await app.open();
        await dialog.config.primary_action();await dialog.config.primary_action();
        assert.equal(app.calls.filter(call=>call.key==='project_send').length,1);
        assert.equal(dialog.button.disabled,true);assert.equal(app.reloads(),0);
        assert.match(dialog.fields_dict.summary.$wrapper[0].textContent,/Sending stopped|could not be confirmed/);
    }
});
test('Project hooks mount only unified action',()=>{
    const hooks=readFileSync(new URL('../hooks.py',import.meta.url),'utf8');
    assert.ok(hooks.includes('"Project": ["public/js/hero_project_send.js"]'));
});
test('Customer script cannot register a second Project field action after visiting Customer',()=>{
    const source=readFileSync(new URL('../public/js/hero_writeback.js',import.meta.url),'utf8');
    const registered=[];
    vm.runInNewContext(source,{frappe:{ui:{form:{on:(doctype)=>registered.push(doctype)}}}});
    assert.deepEqual(registered,['Customer']);
});
test('Frappe hidden phase has no input and still previews and sends',async()=>{
    const app=await setup();const dialog=await app.open();
    assert.equal(dialog.fields_dict.phase.df.hidden,true);
    assert.equal(dialog.fields_dict.phase.$input,undefined);
    assert.equal(app.calls[0].key,'project_send_preview');
    assert.equal(dialog.button.disabled,false);
    await dialog.config.primary_action();assert.equal(app.calls[1].key,'project_send');
});
test('revealing a supported phase creates its input and allows a fresh preview',async()=>{
    const app=await setup({project_send_preview:()=>({...preview(),phases:[{id:7,name:'Installation'}]})});
    const dialog=await app.open();assert.equal(dialog.fields_dict.phase.df.hidden,false);
    assert.ok(dialog.fields_dict.phase.$input);
    await dialog.fields_dict.phase.set_value('7');await app.flush();
    assert.equal(app.calls[1].key,'project_send_preview');assert.equal(app.calls[1].args.phase_id,'7');
    assert.equal(dialog.button.disabled,false);
});
test('related binding conflicts show their actual reason and block confirmation',async()=>{
    const app=await setup({project_send_preview:()=>({...preview(),can_send:false,
        conflicts:[{label:'Customer',blocked_reason:'The native binding requires synchronization or conflict review.'}]})});
    const dialog=await app.open();
    assert.match(dialog.fields_dict.summary.$wrapper[0].textContent,/synchronization or conflict review/);
    assert.match(dialog.fields_dict.summary.$wrapper[0].textContent,/Customer/);
    assert.equal(dialog.button.disabled,true);
});
