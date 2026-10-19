/* Preserved HERO workspace rendered against native saved Frappe state. */
(() => {
    if (window.hero_workspace_tools) return;

    /*
     * Full Graph Viewer cache.
     *
     * Key authority remains Workspace.scopeKey():
     * connection_id + binding_generation + schema_fingerprint.
     *
     * Therefore:
     * - browser reload: same key -> persistent cache hit
     * - new HERO snapshot: new fingerprint -> cache miss
     *
     * IndexedDB failure must never prevent HERO tools from loading.
     */
    const persistentGraphCache = (() => {
        const DB_NAME = 'hero-graph-viewer-cache';
        const STORE_NAME = 'graphs';
        const VERSION = 1;

        function available() {
            return typeof window !== 'undefined'
                && 'indexedDB' in window
                && window.indexedDB;
        }

        function open() {
            if (!available()) {
                return Promise.resolve(null);
            }

            return new Promise(resolve => {
                let request;

                try {
                    request = window.indexedDB.open(
                        DB_NAME,
                        VERSION
                    );
                } catch (_) {
                    resolve(null);
                    return;
                }

                request.onupgradeneeded = () => {
                    const db = request.result;

                    if (
                        !db.objectStoreNames.contains(
                            STORE_NAME
                        )
                    ) {
                        db.createObjectStore(STORE_NAME);
                    }
                };

                request.onsuccess = () =>
                    resolve(request.result);

                request.onerror = () =>
                    resolve(null);

                request.onblocked = () =>
                    resolve(null);
            });
        }

        function valid(value) {
            return !!value
                && Array.isArray(value.nodes)
                && Array.isArray(value.edges)
                && !!value.coverage
                && typeof value.coverage === 'object';
        }

        async function get(key) {
            const db = await open();

            if (!db) return null;

            try {
                const value = await new Promise(resolve => {
                    let request;

                    try {
                        const tx = db.transaction(
                            STORE_NAME,
                            'readonly'
                        );

                        request = tx
                            .objectStore(STORE_NAME)
                            .get(key);
                    } catch (_) {
                        resolve(null);
                        return;
                    }

                    request.onsuccess = () =>
                        resolve(request.result ?? null);

                    request.onerror = () =>
                        resolve(null);
                });

                return valid(value) ? value : null;
            } finally {
                db.close();
            }
        }

        async function set(key, value) {
            if (!valid(value)) return;

            const db = await open();

            if (!db) return;

            try {
                await new Promise(resolve => {
                    let tx;

                    try {
                        tx = db.transaction(
                            STORE_NAME,
                            'readwrite'
                        );

                        tx.objectStore(STORE_NAME)
                            .put(value, key);
                    } catch (_) {
                        resolve();
                        return;
                    }

                    tx.oncomplete = () => resolve();
                    tx.onerror = () => resolve();
                    tx.onabort = () => resolve();
                });
            } finally {
                db.close();
            }
        }

        return Object.freeze({
            get,
            set,
        });
    })();
    const $ = (tag, cls, value) => { const n = document.createElement(tag); n.className = cls || ''; if (value !== undefined) n.textContent = value; return n; };
    const svg = (tag, attrs = {}) => { const n = document.createElementNS('http://www.w3.org/2000/svg', tag); for (const [k,v] of Object.entries(attrs)) n.setAttribute(k,v); return n; };
    const field = (label, input) => { const n = $('label', 'ht-field', label); n.append(input); input.setAttribute('aria-label', label); return n; };
    const input = (placeholder = '') => { const n = $('input', 'form-control'); n.placeholder = placeholder; return n; };
    const button = (label, action) => { const b = $('button', 'btn btn-default btn-sm', label); b.type = 'button'; b.addEventListener('click', action); return b; };
    const errors = {CONFIGURATION_REQUIRED:'Die HERO-Verbindung ist noch nicht vollständig eingerichtet.', PERMISSION_DENIED:'Keine Berechtigung für diese Werkzeugaktion.',
        BINDING_CHANGED:'Die Verbindung wurde geändert. Kontext neu laden und bewusst übernehmen.', REVISION_CONFLICT:'Der Datensatz wurde inzwischen geändert. Neu laden; dein Entwurf bleibt erhalten.',
        SCHEMA_CHANGED:'Das aktuelle Schema wurde geändert. Erneut prüfen.', SCHEMA_NOT_FOUND:'Für diese Verbindung und Revision liegt kein Schema vor.',
        SCHEMA_ANALYSIS_FAILED:'Der vollständige Schemaabruf ist fehlgeschlagen. Kein Ersatzschema wurde erzeugt.', READ_ONLY_BLOCKED:'Diese Operation kann hier nicht ausgeführt werden.',
        SERVICE_KIND_MISMATCH:'Die Operation gehört in die andere Service-Ansicht (Query/Mutation).',
        INVALID_QUERY:'Der GraphQL-Text passt nicht zum gespeicherten Schema.', ANALYZER_OPERATION_REQUIRED:'Eine Query- oder Mutation-Operation auswählen.',
        INVALID_SERVICE_KIND:'Query oder Mutation auswählen.', SUBSCRIPTION_NOT_SUPPORTED:'Subscriptions werden nicht als Service unterstützt.',
        HERO_MUTATIONS_DISABLED:'HERO-Mutationen sind in den Verbindungseinstellungen deaktiviert.', MUTATION_APPROVAL_REQUIRED:'Die konkrete Mutation muss vor der Ausführung bestätigt werden.',
        CONNECTOR_DISABLED:'Die HERO-Verbindung ist deaktiviert.', TOOL_INTERNAL_ERROR:'Die Werkzeugaktion konnte serverseitig nicht abgeschlossen werden.'};
    async function call(method, args = {}, write = false, signal) {
        return window.hero_integration_rpc.call(method, args, {write, signal});
    }
    class Workspace {
        constructor(host, options = {}) {
            this.host = host; this.options = options || {}; this.serviceKind = 'query'; this.root = $('section', 'hero-tools'); this.sequence = 0; this.starts = {analyzer:0,graph:0,services:0,candidates:0}; this.viewbox = null;
            // Tool-local HERO header removed: connection/schema actions live outside the tool views.
            // Keep the existing state/action objects available for shared logic without rendering a duplicate toolbar.
            this.status = $('p', 'ht-context'); this.status.hidden = true; this.notice = $('div', 'ht-notice'); this.notice.setAttribute('role','status');
            this.bindButton = button('Apply Connection',()=>this.run(async()=>{await call('synchronize_connection',{expected_generation:this.context?.binding_generation || 0},true);await this.show(true);}));
            this.refreshButton = button('Refresh Schema',()=>this.run(async()=>{const r=await call('refresh_schema',this.scope(),true);this.schema=r.schema_fingerprint;this.validation=null;await this.show(true);}));
            this.bindButton.hidden = true; this.refreshButton.hidden = true;
            this.tabs = $('nav','pc-shell-nav');this.tabs.setAttribute('aria-label','HERO Tools');
            this.panels = Object.fromEntries(['analyzer','graph','services'].map(k=>[k,$('section','ht-panel')]));
            this.root.append(this.tabs,this.notice,...Object.values(this.panels));
            this.buildAnalyzer();this.buildGraph();this.buildServices();
            // Graph and Analyzer do not require the GraphQL/Ace editor.
            // Initialize it lazily only when GraphQL Services is actually opened.
            this.ready = Promise.resolve();
            this.editorInit = null;
            this.beforeUnloadHandler=e=>{if(this.dirty){e.preventDefault();e.returnValue='';}};
            window.addEventListener('beforeunload',this.beforeUnloadHandler);
            this.resize = new ResizeObserver(()=>{this.editor?.resize();this.resizeGraph(this.canvas);if(this.focusOpen)this.resizeGraph(this.focusCanvas);}); this.resize.observe(this.editorHost);this.resize.observe(this.canvas);
            // CSS variables handle themes. Neither a theme observer nor editor recreation is needed.
        }
        async run(action) {if(this.busy)return;this.busy=true;this.updateActions();this.notice.classList.remove('text-danger');try{this.notice.textContent='Lädt …';await action();if(this.notice.textContent==='Lädt …')this.notice.textContent='';}catch(e){this.notice.textContent=e.message;this.notice.classList.add('text-danger');if(this.tool==='services'){this.validation=null;this.queryOutput.textContent=e.message;}}finally{this.busy=false;this.updateActions();}}
        scope(schema=false) {return {connection_id:this.context.connection_id,binding_generation:this.context.binding_generation,...(schema?{schema_fingerprint:this.schema}: {})};}
        async show(force=false) {
            this.host.append(this.root);await this.ready;const serial=++this.sequence;this.cancelGraph();this.focusDialog?.hide();
            const params=new URLSearchParams(location.search), requested=this.options.tool || params.get('tool') || 'analyzer';
            this.tool=Object.hasOwn(this.panels,requested)?requested:'analyzer';this.root.classList.toggle('is-analyzer',this.tool==='analyzer');
            if(this.tool==='services'&&!this.editor){
                this.editorInit=this.editorInit||this.initializeEditor();
                this.ready=this.editorInit;
                await this.ready;
            }
            const oldConnection=this.context?`${this.context.connection_id}:${this.context.binding_generation}`:'';
            const oldScope=this.context ? `${this.context.connection_id}:${this.context.binding_generation}:${this.schema}` : '';
            const context=await call('get_context');
            if(serial!==this.sequence)return;this.context=context;
            if(oldConnection!==`${this.context.connection_id}:${this.context.binding_generation}`)this.queryEpoch=(this.queryEpoch||0)+1;
            this.schema=params.get('schema') || this.context.schema_fingerprint;
            if(oldScope!==`${this.context.connection_id}:${this.context.binding_generation}:${this.schema}`){
                this.validation=null;this.graphCache=null;this.graph=null;this.selectedNode=null;this.viewbox=null;
                this.canvas.replaceChildren();this.canvas._heroScene=null;this.typeDetail.replaceChildren();this.nodeDetail.replaceChildren();this.candidates.replaceChildren();
            }
            this.status.textContent=`Source HERO · Verbindung ${this.context.connection_id || 'not bound'} · Bindung ${this.context.binding_generation} · Schema ${this.schema || 'not available'}`;
            this.bindButton.hidden=!this.context.needs_binding_update;this.refreshButton.disabled=!this.context.can_write || !this.context.enabled || this.context.needs_binding_update;
            this.tabs.replaceChildren();for(const [key,label] of [['analyzer','API Analyzer'],['graph','Graph Viewer'],['services','GraphQL Services']]) {
                const link=$('a','pc-shell-link'+(key===this.tool?' is-current':''),label);
                const target=new URL(location.href);target.searchParams.set('tool',key);link.href=target.pathname+target.search+target.hash;
                this.tabs.append(hero_integration_shell.navigationLink(link));
            }
            for(const [key,panel] of Object.entries(this.panels))panel.hidden=key!==this.tool;
            this.editor?.setReadOnly(!this.context.can_write);this.updateActions();
            if(this.context.needs_binding_update){this.notice.textContent='Configuration changed or is new. Apply the connection before using the tools.';return;}
            if(!this.schema && this.tool!=='services'){this.notice.textContent='No schema is available for this connection. Refresh Schema retrieves the defined introspection only.';return;}
            this.notice.textContent='';await this.loadTool(serial);this.editor?.resize();
        }
        async loadTool(serial=this.sequence) {
            if(this.tool==='analyzer')await this.loadAnalyzer(serial);
            if(this.tool==='graph'){await this.loadGraph(serial);await this.loadCandidates(serial);}
            if(this.tool==='services')await this.loadQueries(serial);
        }
        pager(key, reload) {
            const p=$('div','ht-toolbar'),back=button('Previous',()=>{this.starts[key]=Math.max(0,this.starts[key]-50);this.run(reload);}),next=button('Next',()=>{this.starts[key]+=50;this.run(reload);});
            p.append(back,next);return {root:p,update:more=>{back.disabled=!this.starts[key];next.disabled=!more;}};
        }
        analyzerKindSpecs(){return [
            {kind:'',label:'All Types'},
            {kind:'OBJECT',label:'Object Types'},
            {kind:'INPUT_OBJECT',label:'Input Objects'},
            {kind:'INTERFACE',label:'Interfaces'},
            {kind:'UNION',label:'Unions'},
            {kind:'ENUM',label:'Enums'},
            {kind:'SCALAR',label:'Scalars'}
        ];}
        analyzerRootName(roots,family){
            if(!roots)return '—';const keys=Object.keys(roots),needle=family.toLocaleLowerCase(),key=keys.find(k=>k.toLocaleLowerCase()===needle)||keys.find(k=>k.toLocaleLowerCase().includes(needle));if(!key)return '—';const value=roots[key];
            if(typeof value==='string')return value;if(value&&typeof value==='object')return value.name||value.type||value.type_name||value.node_id||JSON.stringify(value);return value==null?'—':String(value);
        }
        analyzerDirectiveRows(directives){
            if(Array.isArray(directives))return directives;if(directives&&typeof directives==='object')return Object.entries(directives).map(([name,value])=>typeof value==='object'&&value?{name,...value}:{name,value});return [];
        }
        async analyzerOverview(scope,current,serial,generation){
            const key=this.scopeKey(scope);if(this.analyzerOverviewCache?.key===key)return this.analyzerOverviewCache.data;
            const unfiltered=!this.typeSearch.value.trim()&&!this.typeKind.value&&!this.starts.analyzer,currentBase=unfiltered?current:null;
            const basePromise=currentBase?Promise.resolve(currentBase):call('get_schema',{...scope,search:'',kind:'',start:0,page_size:1});
            const kinds=this.analyzerKindSpecs().filter(item=>item.kind);const [base,...counts]=await Promise.all([basePromise,...kinds.map(item=>call('get_schema',{...scope,search:'',kind:item.kind,start:0,page_size:1}))]);
            if(serial!==this.sequence||generation!==this.analyzerGeneration||key!==this.scopeKey())return null;
            const totals=Object.fromEntries(kinds.map((item,index)=>[item.kind,counts[index].total]));const overview={base,totals};this.analyzerOverviewCache={key,data:overview};return overview;
        }
        buildAnalyzer() {
            const panel=this.panels.analyzer,bar=$('div','ht-toolbar ht-analyzer-toolbar');panel.classList.add('ht-analyzer-panel');this.typeSearch=input('Search Type');this.typeKind=$('select','form-control');
            for(const item of this.analyzerKindSpecs()){const o=$('option','',item.label);o.value=item.kind;this.typeKind.append(o);}
            const search=()=>{clearTimeout(this.analyzerSearchTimer);this.analyzerSearchTimer=null;this.starts.analyzer=0;this.run(()=>this.loadAnalyzer());},quietSearch=()=>{const serial=this.sequence;this.loadAnalyzer(serial).catch(error=>{if(serial===this.sequence&&this.tool==='analyzer'){this.notice.textContent=error.message;this.notice.classList.add('text-danger');}});},liveSearch=()=>{clearTimeout(this.analyzerSearchTimer);this.starts.analyzer=0;this.analyzerSearchTimer=setTimeout(()=>{this.analyzerSearchTimer=null;if(this.tool!=='analyzer')return;if(this.busy){liveSearch();return;}quietSearch();},220);},reset=()=>{clearTimeout(this.analyzerSearchTimer);this.analyzerSearchTimer=null;this.typeSearch.value='';this.typeKind.value='';this.starts.analyzer=0;this.run(()=>this.loadAnalyzer());};
            this.typeSearch.addEventListener('input',liveSearch);this.typeSearch.addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();search();}});this.typeKind.addEventListener('change',search);
            bar.append(field('Type Name',this.typeSearch),field('Type Class',this.typeKind),button('Search',search),button('Reset',reset));
            this.analyzerSummary=$('section','ht-analyzer-summary');this.analyzerCategories=$('nav','ht-analyzer-categories');this.analyzerCategories.setAttribute('aria-label','API Type Categories');this.analyzerMeta=$('div','ht-analyzer-meta');
            const top=$('div','ht-analyzer-top');top.append(bar,this.analyzerSummary);
            this.types=$('div','ht-list ht-analyzer-types');this.types.setAttribute('role','listbox');this.typeDetail=$('aside','ht-inspector ht-analyzer-detail');const grid=$('div','ht-split ht-analyzer-split');grid.append(this.types,this.typeDetail);
            this.analyzerPager=this.pager('analyzer',()=>this.loadAnalyzer());panel.append(top,this.analyzerCategories,this.analyzerMeta,grid,this.analyzerPager.root);
        }
        renderAnalyzerOverview(overview,scope,serial){
            const base=overview.base,directives=this.analyzerDirectiveRows(base.directives),rootQuery=this.analyzerRootName(base.roots,'query'),rootMutation=this.analyzerRootName(base.roots,'mutation'),rootSubscription=this.analyzerRootName(base.roots,'subscription');
            const head=$('div','ht-analyzer-summary-head');head.append($('div','', 'Schema Overview'));const fingerprint=$('span','ht-analyzer-fingerprint',this.schema?`Schema ${this.schema.slice(0,12)}…`:'Schema —');fingerprint.title=this.schema||'';const state=$('span','ht-analyzer-state '+(base.complete_analysis?'is-complete':'is-partial'),base.complete_analysis?'Complete Introspection':'Partial State');head.append(fingerprint,state);
            const stats=$('div','ht-analyzer-stats'),stat=(label,value)=>{const card=$('div','ht-analyzer-stat');card.append($('span','',label),$('strong','',String(value??'—')));return card;};
            stats.append(stat('Total Types',base.total),stat('Query Root',rootQuery),stat('Mutation Root',rootMutation),stat('Subscription Root',rootSubscription),stat('Directives',directives.length),stat('Captured',base.captured_at||'—'));this.analyzerSummary.replaceChildren(head,stats);
            this.analyzerCategories.replaceChildren();for(const item of this.analyzerKindSpecs()){const count=item.kind?overview.totals[item.kind]:base.total,b=button('',()=>{this.typeSearch.value='';this.typeKind.value=item.kind;this.starts.analyzer=0;this.run(()=>this.loadAnalyzer());});b.className='ht-analyzer-category'+(this.typeKind.value===item.kind?' is-active':'');b.dataset.kind=item.kind||'ALL';b.setAttribute('aria-pressed',String(this.typeKind.value===item.kind));b.append($('span','ht-analyzer-category-label',item.label),$('strong','ht-analyzer-category-count',String(count)));this.analyzerCategories.append(b);}
            const roots=$('details','ht-analyzer-meta-card'),rootSummary=$('summary','', 'Roots');roots.append(rootSummary);const rootList=$('div','ht-analyzer-root-list');for(const [label,name] of [['Query',rootQuery],['Mutation',rootMutation],['Subscription',rootSubscription]]){const row=$('div','ht-analyzer-meta-row');row.append($('strong','',label),name&&name!=='—'?button(name,()=>this.analyzerNode('root:'+label.toLowerCase(),scope,serial)):$('span','',name));rootList.append(row);}roots.append(rootList);
            const directiveBox=$('details','ht-analyzer-meta-card'),directiveSummary=$('summary','',`Directives (${directives.length})`),directiveList=$('div','ht-analyzer-directives');directiveBox.append(directiveSummary);if(directives.length){for(const directive of directives){const name=typeof directive==='string'?directive:directive?.name||directive?.directive||JSON.stringify(directive);directiveList.append($('span','ht-analyzer-directive',name));}}else directiveList.append($('span','ht-context','Keine Directives im gespeicherten Schema.'));directiveBox.append(directiveList);
            this.analyzerMeta.replaceChildren(roots,directiveBox);
        }
        markAnalyzerTypeSelection(name=this.selectedType){
            for(const item of this.types.querySelectorAll('.ht-analyzer-type')){const selected=!!name&&item.dataset.name===name;item.classList.toggle('is-selected',selected);item.setAttribute('aria-selected',String(selected));if(selected)item.setAttribute('aria-current','true');else item.removeAttribute('aria-current');}
        }
        renderAnalyzerTypes(data,scope,serial){
            const header=$('div','ht-analyzer-list-head');header.append($('h3','', 'API Types'),$('span','ht-context',`${data.total} Treffer${this.typeSearch.value.trim()?' · Suche: '+this.typeSearch.value.trim():''}`));this.types.replaceChildren(header);
            if(!data.types.length)this.types.append($('p','ht-context','No types match this filter.'));
            let selectedOnPage=null;for(const type of data.types){const b=button('',()=>this.showAnalyzerType(type,scope,serial));b.className='ht-analyzer-type';b.dataset.kind=type.kind;b.dataset.name=type.name;b.setAttribute('role','option');b.setAttribute('aria-selected','false');b.append($('span','ht-analyzer-type-name',type.name),$('span','ht-analyzer-type-kind',type.kind));this.types.append(b);if(this.selectedType===type.name)selectedOnPage=type;}
            this.markAnalyzerTypeSelection();if(selectedOnPage&&!this.typeDetail.childNodes.length)this.showAnalyzerType(selectedOnPage,scope,serial);else if(!this.selectedType&&!this.typeDetail.childNodes.length)this.typeDetail.replaceChildren($('h3','', 'Type Details'),$('p','ht-context','Select a type on the left to inspect its definition and relations.'));
        }
        async loadAnalyzer(serial=this.sequence) {
            const generation=this.analyzerGeneration=(this.analyzerGeneration||0)+1,scope=this.scope(true);
            const data=await call('get_schema',{...scope,search:this.typeSearch.value,kind:this.typeKind.value,start:this.starts.analyzer,page_size:50});if(serial!==this.sequence||generation!==this.analyzerGeneration)return;
            const overview=await this.analyzerOverview(scope,data,serial,generation);if(!overview)return;this.renderAnalyzerOverview(overview,scope,serial);this.renderAnalyzerTypes(data,scope,serial);this.analyzerPager.update(data.has_more);
        }
        async showAnalyzerType(type,scope,serial){
            const generation=this.analyzerDetailGeneration=(this.analyzerDetailGeneration||0)+1;this.selectedType=type.name;this.markAnalyzerTypeSelection(type.name);this.typeDetail.replaceChildren($('p','','Loading details …'));
            try {const data=await this.nodeData('type:'+type.name,scope);if(serial!==this.sequence||generation!==this.analyzerDetailGeneration||this.scopeKey(scope)!==this.scopeKey())return;
                this.detailContents(this.typeDetail,data,scope,id=>this.analyzerNode(id,scope,serial));this.typeDetail.append($('pre','ht-data',JSON.stringify(type,null,2)));
            } catch(error){if(serial===this.sequence&&generation===this.analyzerDetailGeneration)this.typeDetail.replaceChildren($('p','text-danger',error.message));}
        }
        async analyzerNode(id,scope,serial){
            const generation=this.analyzerDetailGeneration=(this.analyzerDetailGeneration||0)+1;
            try {const data=await this.nodeData(id,scope);if(serial!==this.sequence||generation!==this.analyzerDetailGeneration||this.scopeKey(scope)!==this.scopeKey())return;this.detailContents(this.typeDetail,data,scope,next=>this.analyzerNode(next,scope,serial));}
            catch(error){if(serial===this.sequence&&generation===this.analyzerDetailGeneration)this.typeDetail.replaceChildren($('p','text-danger',error.message));}
        }
        scopeKey(scope=this.scope(true)){return JSON.stringify([scope.connection_id,scope.binding_generation,scope.schema_fingerprint]);}
        edgeKey(edge){return JSON.stringify([edge.source_id,edge.target_id,edge.kind,edge.label??null]);}
        isBuiltinScalar(node){return node?.kind==='SCALAR'&&new Set(['Int','String','ID','Boolean','Float']).has(node.name);}
        cachedNode(id){return this.graphCache?.details?.get(id)?.node||null;}
        scalarUsageRows(detail){
            const groups=new Map(),add=(edge,direction,partnerId)=>{const key=`${edge.kind}:${direction}`;if(!groups.has(key))groups.set(key,{kind:edge.kind,direction,rows:[]});const node=this.cachedNode(partnerId);groups.get(key).rows.push({edge,partnerId,node,label:node?.name||partnerId});};
            for(const edge of detail.incoming)add(edge,'incoming',edge.source_id);for(const edge of detail.outgoing)add(edge,'outgoing',edge.target_id);
            const directionLabel={incoming:'Incoming',outgoing:'Outgoing'},compare=(a,b)=>a.localeCompare(b,'de',{numeric:true,sensitivity:'base'});
            return [...groups.values()].sort((a,b)=>compare(a.kind,b.kind)||compare(directionLabel[a.direction],directionLabel[b.direction])).map(group=>({...group,rows:group.rows.sort((a,b)=>compare(a.label,b.label)||compare(a.partnerId,b.partnerId))}));
        }
        setFocusGraphMode(graphMode){
            this.focusCanvas.hidden=!graphMode;this.focusScalar.hidden=graphMode;
            for(const control of [this.focusFit,this.focusZoomIn,this.focusZoomOut])control.hidden=!graphMode;
        }
        scalarDetailContents(target,detail,scope){
            const groups=this.scalarUsageRows(detail),total=groups.reduce((sum,group)=>sum+group.rows.length,0);
            target.replaceChildren($('h3','',detail.node.name),$('p','',detail.node.node_id),$('p','',detail.node.description||''),$('pre','ht-data',JSON.stringify(detail.node,null,2)));this.appendMark(target,detail.node,scope);
            target.append($('h4','',`Verwendungen (${total})`));
            for(const group of groups)target.append($('p','ht-scalar-count',`${group.kind} · ${group.direction==='incoming'?'eingehend':'ausgehend'} · ${group.rows.length}`));
        }
        renderScalarFocus(detail){
            const groups=this.scalarUsageRows(detail),total=groups.reduce((sum,group)=>sum+group.rows.length,0),root=$('div','ht-scalar-focus-content');
            const heading=$('div','ht-scalar-heading');heading.append($('div','ht-scalar-badge','SCALAR'),$('h3','',detail.node.name),$('p','ht-context',`${total} direkte Verwendungen`));root.append(heading);
            root.append($('p','ht-scalar-explainer','GraphQL-Basisskalar: Verwendungen werden als Liste statt als Beziehungsgraph dargestellt.'));
            const search=input('Verwendungen filtern');const searchField=field('Verwendungen filtern',search),status=$('p','ht-context',`${total} Verwendungen`);root.append(searchField,status);
            const groupNodes=[];
            for(const group of groups){const details=$('details','ht-scalar-group'),summary=$('summary','',`${group.kind} · ${group.direction==='incoming'?'Incoming':'Outgoing'} (${group.rows.length})`),body=$('div','ht-scalar-usage-list');details.append(summary,body);
                const rowNodes=[];for(const row of group.rows){const meta=row.node?`${row.node.kind}${row.edge.label?' · '+row.edge.label:''}`:`${row.edge.label||row.partnerId}`,b=button(`${row.label} · ${meta}`,()=>this.focusNext(row.partnerId));b.classList.add('ht-scalar-usage');b.dataset.search=`${row.label} ${row.partnerId} ${row.node?.kind||''} ${row.edge.kind} ${row.edge.label||''}`.toLocaleLowerCase();body.append(b);rowNodes.push(b);}
                root.append(details);groupNodes.push({details,rowNodes});
            }
            search.addEventListener('input',()=>{const q=search.value.trim().toLocaleLowerCase();let visible=0;for(const group of groupNodes){let groupVisible=0;for(const row of group.rowNodes){const show=!q||row.dataset.search.includes(q);row.hidden=!show;if(show){visible++;groupVisible++;}}group.details.hidden=!groupVisible;if(q&&groupVisible)group.details.open=true;}status.textContent=`${visible} von ${total} Verwendungen`;});
            this.focusScalar.replaceChildren(root);this.focusNotice.textContent=`${detail.node.name} · ${total} direkte Verwendungen · Listenansicht für GraphQL-Basisskalar`;
        }
        buildGraph() {
            const panel=this.panels.graph,bar=$('div','ht-toolbar');this.graphSearch=input('Name, ID or type');this.graphKind=$('select','form-control');this.relationKind=$('select','form-control');
            for(const value of ['','QUERY_OPERATION','MUTATION_OPERATION','SUBSCRIPTION_OPERATION','QUERY_ROOT','MUTATION_ROOT','SUBSCRIPTION_ROOT','OBJECT','INPUT_OBJECT','INTERFACE','UNION','ENUM','ENUM_VALUE','SCALAR','FIELD','ARGUMENT','INPUT_FIELD','DIRECTIVE']){const o=$('option','',value||'All nodes');o.value=value;this.graphKind.append(o);}
            for(const value of ['','RETURNS','HAS_FIELD','HAS_ARGUMENT','ARGUMENT_TYPE','INPUT_TYPE','IMPLEMENTS','POSSIBLE_TYPE','ROOT_TYPE','ROOT_HAS_OPERATION','HAS_INPUT_FIELD','HAS_ENUM_VALUE']){const o=$('option','',value||'All relations');o.value=value;this.relationKind.append(o);}
            const filtered=()=>this.loadGraph(),all=()=>{this.graphSearch.value='';this.graphKind.value='';this.relationKind.value='';this.loadGraph();};
            this.graphSearch.addEventListener('input',()=>{this.cancelGraph();this.graphInfo.textContent='Filter changed. Filtered View loads the current selection.';});
            for(const control of [this.graphKind,this.relationKind])control.addEventListener('change',filtered);
            bar.append(button('Full Schema',all),field('Graph Search',this.graphSearch),field('Node Class',this.graphKind),field('Relation',this.relationKind),button('Filtered View',filtered),button('Fit',()=>{this.viewbox=null;this.drawGraph();}),button('+',()=>this.zoom(.8)),button('−',()=>this.zoom(1.25)));
            this.graphInfo=$('p');this.graphInfo.setAttribute('role','status');this.canvas=svg('svg',{'class':'ht-graph',height:650,'aria-label':'HERO API Graph'});
            this.bindGraphNavigation(this.canvas,()=>this.viewbox,box=>{this.viewbox=box;},()=>this.applyViewbox(),factor=>this.zoom(factor));
            this.nodeDetail=$('aside','ht-inspector');this.candidates=$('div','ht-candidates');const side=$('aside','ht-graph-side');side.append(this.nodeDetail,this.candidates);
            const grid=$('div','ht-split');grid.append(this.canvas,side);
            this.labelNotice=$('p','ht-legend ht-label-note');this.labelNotice.setAttribute('aria-live','polite');
            const legend=$('div','ht-legend ht-category-legend');for(const [kind,label] of [['QUERY_OPERATION','Query'],['MUTATION_OPERATION','Mutation'],['INPUT_OBJECT','Eingaben'],['ENUM','Enum'],['OBJECT','Weitere Schematypen']]){const item=$('span','',label);item.dataset.kind=kind;legend.append(item);}
            panel.append(bar,legend,$('p','ht-legend','Pfeile zeigen Richtung und Beziehungstyp. Ein Klick öffnet den Knotenfokus; jede Vormerkung bleibt ein Vorschlag.'),this.graphInfo,this.labelNotice,grid);
        }
        bindGraphNavigation(canvas,getBox,setBox,applyBox,zoom){
            canvas.style.touchAction='none';let pan=null;
            canvas.addEventListener('pointerdown',e=>{const box=getBox();if(e.button!==0||e.target.closest('[data-node],[data-overview-kind]')||!box)return;pan={pointerId:e.pointerId,x:e.clientX,y:e.clientY,box:[...box]};canvas.setPointerCapture(e.pointerId);});
            canvas.addEventListener('pointermove',e=>{if(!pan||e.pointerId!==pan.pointerId)return;const r=canvas.getBoundingClientRect();if(!r.width||!r.height)return;setBox([pan.box[0]-(e.clientX-pan.x)*pan.box[2]/r.width,pan.box[1]-(e.clientY-pan.y)*pan.box[3]/r.height,pan.box[2],pan.box[3]]);applyBox();});
            const stop=e=>{if(!pan||e.pointerId!==pan.pointerId)return;pan=null;};
            canvas.addEventListener('pointerup',stop);canvas.addEventListener('pointercancel',stop);
            canvas.addEventListener('wheel',e=>{if(!getBox())return;e.preventDefault();zoom(e.deltaY>0?1.12:.9);},{passive:false});
        }
        cancelGraph(){this.graphGeneration=(this.graphGeneration||0)+1;this.graphAbort?.abort();}
        async loadGraph(serial=this.sequence) {
            this.cancelGraph();const generation=this.graphGeneration,controller=new AbortController();this.graphAbort=controller;
            const scope=this.scope(true),key=this.scopeKey(scope),filter={search:this.graphSearch.value.trim().toLocaleLowerCase(),kind:this.graphKind.value,relation:this.relationKind.value};
            const current=()=>serial===this.sequence&&generation===this.graphGeneration&&key===this.scopeKey()&&!controller.signal.aborted;
            // Never expose a partially loaded projection as the requested graph.
            this.canvas.replaceChildren();this.canvas._heroScene=null;this.graph=null;this.nodeDetail.replaceChildren();this.labelNotice.textContent='';
            this.graphInfo.textContent='Schema laden · Knoten und seitenübergreifende Beziehungen werden zusammengeführt …';
            try {
                let full=this.graphCache?.key===key
                    ? this.graphCache.data
                    : null;

                if(!full){
                    full=await persistentGraphCache.get(key);
                    if(!current())return;

                    if(full){
                        this.graphCache={
                            key,
                            data:full
                        };
                    }
                }

                if(!full){
                    const nodes=new Map(),edges=new Map(),argumentOwners=new Set(),argumentTypes=new Set();let coverage=null,start=0;
                    const addEdges=list=>{for(const edge of list){edges.set(this.edgeKey(edge),edge);if(edge.kind==='HAS_ARGUMENT')argumentOwners.add(edge.target_id);if(edge.kind==='ARGUMENT_TYPE')argumentTypes.add(edge.source_id);}};
                    while(true){
                        const page=await call('get_graph',{...scope,start,page_size:200},false,controller.signal);if(!current())return;
                        if(page.schema_fingerprint!==scope.schema_fingerprint||!Array.isArray(page.nodes)||!Array.isArray(page.edges))throw new Error('Schemaantwort passt nicht zum angeforderten Kontext.');
                        coverage=coverage||page.coverage;
                        if(page.total!==coverage.nodes||page.coverage.nodes!==coverage.nodes||page.coverage.edges!==coverage.edges)throw new Error('Graphumfang hat sich während des Ladens geändert.');
                        for(const node of page.nodes){if(nodes.has(node.node_id))throw new Error('Doppelte Knotenidentität im Seitenvertrag.');nodes.set(node.node_id,node);}addEdges(page.edges);
                        this.graphInfo.textContent=`Lädt · ${nodes.size}/${coverage.nodes} Knoten · ${edges.size}/${coverage.edges} Beziehungen`;
                        if(!page.has_more)break;if(!page.nodes.length)throw new Error('Leere Folgeseite im Graphvertrag.');start+=page.nodes.length;
                    }
                    if(nodes.size!==coverage.nodes)throw new Error('Nicht alle Knoten wurden geliefert.');
                    // Page edges omit cross-page relations. Complete them through the existing
                    // detail contract; no client-side schema projection or inferred edges.
                    const priority=new Set(['OBJECT','INPUT_OBJECT','SCALAR','ENUM','INTERFACE','UNION','QUERY_ROOT','MUTATION_ROOT','SUBSCRIPTION_ROOT']);
                    const containers=new Set(['QUERY_OPERATION','MUTATION_OPERATION','SUBSCRIPTION_OPERATION','DIRECTIVE']);
                    const rank=n=>priority.has(n.kind)?0:containers.has(n.kind)?1:n.kind==='ARGUMENT'?2:3;
                    const pending=[...nodes.values()].sort((a,b)=>rank(a)-rank(b)),deferred=[];let cursor=0,deferredCursor=0;
                    // Already observed argument relations only change read order. Deferred
                    // nodes remain a fallback until the server's edge coverage is complete.
                    const nextNode=()=>{while(cursor<pending.length){const node=pending[cursor++];if(node.kind==='ARGUMENT'&&argumentOwners.has(node.node_id)&&argumentTypes.has(node.node_id)){deferred.push(node);continue;}return node;}return deferred[deferredCursor++];};
                    await Promise.all(Array.from({length:4},async()=>{
                        while(current()&&edges.size<coverage.edges){
                            const next=nextNode();if(!next)return;
                            const id=next.node_id,data=await call('get_graph',{...scope,node_id:id},false,controller.signal);if(!current())return;
                            if(data.schema_fingerprint!==scope.schema_fingerprint||data.node?.node_id!==id)throw new Error('Knotendetail gehört nicht zum angeforderten Schema.');
                            addEdges([...data.incoming,...data.outgoing]);
                            this.graphInfo.textContent=`Lädt · ${nodes.size}/${coverage.nodes} Knoten · ${edges.size}/${coverage.edges} Beziehungen`;
                        }
                    }));
                    if(!current())return;
                    if(edges.size!==coverage.edges||[...edges.values()].some(e=>!nodes.has(e.source_id)||!nodes.has(e.target_id)))throw new Error('Die vollständigen Beziehungen konnten nicht nachgewiesen werden.');
                    full={
                        nodes:[...nodes.values()],
                        edges:[...edges.values()],
                        coverage
                    };

                    this.graphCache={
                        key,
                        data:full
                    };

                    await persistentGraphCache.set(
                        key,
                        full
                    );
                }
                if(!current())return;
                const filtered=!!(filter.search||filter.kind||filter.relation),matches=new Set(full.nodes.filter(n=>(!filter.search||[n.node_id,n.name,n.type_ref||''].join(' ').toLocaleLowerCase().includes(filter.search))&&(!filter.kind||n.kind===filter.kind)).map(n=>n.node_id));
                const allowedEdges=full.edges.filter(e=>!filter.relation||e.kind===filter.relation),ids=new Set(matches);
                // Explicit semantic boundary: matching nodes plus their immediate partners.
                for(const edge of allowedEdges)if(matches.has(edge.source_id)||matches.has(edge.target_id)){ids.add(edge.source_id);ids.add(edge.target_id);}
                const graph={nodes:full.nodes.filter(n=>ids.has(n.node_id)),edges:allowedEdges.filter(e=>ids.has(e.source_id)&&ids.has(e.target_id)),coverage:full.coverage};
                this.graphFilterActive=filtered;
                const signature=JSON.stringify([key,filter]);if(signature!==this.graphSignature)this.viewbox=null;this.graphSignature=signature;this.graph=graph;
                this.graphInfo.textContent=`${filtered?'Gefilterte Ansicht: '+matches.size+' Treffer + direkte Beziehungspartner':'Full Schema'} · ${graph.nodes.length} Knoten · ${graph.edges.length} Beziehungen · vollständig geladen: ${full.nodes.length}/${full.coverage.nodes} Knoten, ${full.edges.length}/${full.coverage.edges} Beziehungen${full.coverage.introspection_complete?'':' · gespeicherte Introspection ist unvollständig'}`;
                if(!ids.has(this.selectedNode))this.selectedNode=null;
                const id=new URLSearchParams(location.search).get('node')||this.selectedNode||graph.nodes[0]?.node_id;
                if(id&&ids.has(id))await this.inspectNode(id,serial,false);else this.drawGraph();
            } catch(error){if(current()){controller.abort();this.graph=null;this.canvas.replaceChildren();this.canvas._heroScene=null;this.graphInfo.textContent='Graph nicht vollständig geladen: '+error.message+' Erneut „Gesamtes Schema“ oder „Gefilterte Ansicht“ wählen.';this.labelNotice.textContent='Keine vollständige Darstellung verfügbar.';}}
        }
        drawGraph(){if(!this.graph)return;this.viewbox=this.renderGraphScene(this.canvas,this.graph,this.selectedNode,this.viewbox,id=>this.inspectNode(id),this.labelNotice,'hero-api-arrow');}
        graphNeedsOverview(graph){return graph.nodes.length>120||graph.edges.length>180;}
        graphSelectionState(graph,selected){
            const neighbors=new Set(),relatedEdges=new Set();if(!selected)return {active:false,neighbors,relatedEdges};
            for(const edge of graph.edges){if(edge.source_id!==selected&&edge.target_id!==selected)continue;relatedEdges.add(this.edgeKey(edge));neighbors.add(edge.source_id===selected?edge.target_id:edge.source_id);}
            return {active:true,neighbors,relatedEdges};
        }
        graphLayout(graph){
            if(!this.graphLayouts)this.graphLayouts=new WeakMap();const cached=this.graphLayouts.get(graph);if(cached)return cached;
            const nodeWidth=250,nodeHeight=70,rowGap=38,columnGap=60,layerGap=110;
            const nodes=graph.nodes,positions=new Map();if(!nodes.length){const empty={positions,width:320,height:300,nodeWidth,nodeHeight};this.graphLayouts.set(graph,empty);return empty;}
            const byId=new Map(nodes.map(node=>[node.node_id,node])),outgoing=new Map(nodes.map(node=>[node.node_id,[]])),incoming=new Map(nodes.map(node=>[node.node_id,[]]));
            const kindOrder=new Map(['QUERY_ROOT','MUTATION_ROOT','SUBSCRIPTION_ROOT','QUERY_OPERATION','MUTATION_OPERATION','SUBSCRIPTION_OPERATION','OBJECT','INPUT_OBJECT','INTERFACE','UNION','ENUM','SCALAR','FIELD','INPUT_FIELD','ARGUMENT','ENUM_VALUE','DIRECTIVE'].map((kind,index)=>[kind,index]));
            const compareText=(a,b)=>a<b?-1:a>b?1:0,stable=(a,b)=>(kindOrder.get(a.kind)??99)-(kindOrder.get(b.kind)??99)||compareText(a.name||'',b.name||'')||compareText(a.node_id,b.node_id);
            const validEdges=[];for(const edge of graph.edges){if(!byId.has(edge.source_id)||!byId.has(edge.target_id))continue;validEdges.push(edge);outgoing.get(edge.source_id).push(edge.target_id);incoming.get(edge.target_id).push(edge.source_id);}
            for(const ids of [...outgoing.values(),...incoming.values()])ids.sort((a,b)=>stable(byId.get(a),byId.get(b)));
            // Collapse directed cycles first. The resulting component graph is acyclic, so
            // relation direction can define stable left-to-right layers without runaway depth.
            let index=0;const indices=new Map(),low=new Map(),stack=[],onStack=new Set(),components=[],componentOf=new Map();
            const visit=id=>{indices.set(id,index);low.set(id,index++);stack.push(id);onStack.add(id);for(const next of outgoing.get(id)){if(!indices.has(next)){visit(next);low.set(id,Math.min(low.get(id),low.get(next)));}else if(onStack.has(next))low.set(id,Math.min(low.get(id),indices.get(next)));}if(low.get(id)===indices.get(id)){const members=[];while(stack.length){const member=stack.pop();onStack.delete(member);members.push(member);if(member===id)break;}members.sort((a,b)=>stable(byId.get(a),byId.get(b)));const c=components.length;components.push(members);for(const member of members)componentOf.set(member,c);}};
            for(const node of [...nodes].sort(stable))if(!indices.has(node.node_id))visit(node.node_id);
            const componentOut=components.map(()=>new Set()),componentIn=components.map(()=>new Set());for(const edge of validEdges){const a=componentOf.get(edge.source_id),b=componentOf.get(edge.target_id);if(a===b)continue;componentOut[a].add(b);componentIn[b].add(a);}
            const componentKey=c=>byId.get(components[c][0]),ready=[];for(let c=0;c<components.length;c++)if(!componentIn[c].size)ready.push(c);ready.sort((a,b)=>stable(componentKey(a),componentKey(b)));
            const depth=new Array(components.length).fill(0),orderedComponents=[];while(ready.length){const c=ready.shift();orderedComponents.push(c);for(const next of [...componentOut[c]].sort((a,b)=>stable(componentKey(a),componentKey(b)))){depth[next]=Math.max(depth[next],depth[c]+1);componentIn[next].delete(c);if(!componentIn[next].size){ready.push(next);ready.sort((a,b)=>depth[a]-depth[b]||stable(componentKey(a),componentKey(b)));}}}
            // Defensive fallback; SCC condensation should have emitted every component.
            for(let c=0;c<components.length;c++)if(!orderedComponents.includes(c))orderedComponents.push(c);
            const layers=new Map();for(const c of orderedComponents){const d=depth[c]||0;if(!layers.has(d))layers.set(d,[]);for(const id of components[c])layers.get(d).push(byId.get(id));}
            const maxRows=Math.max(6,Math.min(12,Math.ceil(Math.sqrt(nodes.length)*1.45))),placedOrder=new Map();let cursorX=40,maxHeight=300,orderCounter=0;
            for(const d of [...layers.keys()].sort((a,b)=>a-b)){const layer=layers.get(d);const score=node=>{const predecessors=incoming.get(node.node_id).filter(id=>positions.has(id));if(!predecessors.length)return Number.POSITIVE_INFINITY;return predecessors.reduce((sum,id)=>sum+(placedOrder.get(id)??0),0)/predecessors.length;};layer.sort((a,b)=>score(a)-score(b)||stable(a,b));
                const subColumns=Math.max(1,Math.ceil(layer.length/maxRows)),rows=Math.min(maxRows,layer.length);layer.forEach((node,i)=>{const column=Math.floor(i/maxRows),row=i%maxRows;positions.set(node.node_id,{x:cursorX+column*(nodeWidth+columnGap),y:40+row*(nodeHeight+rowGap)});placedOrder.set(node.node_id,orderCounter++);});
                maxHeight=Math.max(maxHeight,40+rows*(nodeHeight+rowGap));cursorX+=subColumns*(nodeWidth+columnGap)+layerGap;
            }
            const layout={positions,width:Math.max(320,cursorX-layerGap+40),height:Math.max(300,maxHeight+40),nodeWidth,nodeHeight};this.graphLayouts.set(graph,layout);return layout;
        }
        renderGraphOverview(canvas,graph,selected,box,notice,markerId){
            const labels={QUERY_ROOT:'Query Root',MUTATION_ROOT:'Mutation Root',SUBSCRIPTION_ROOT:'Subscription Root',QUERY_OPERATION:'Queries',MUTATION_OPERATION:'Mutations',SUBSCRIPTION_OPERATION:'Subscriptions',OBJECT:'Object Types',INPUT_OBJECT:'Input Objects',INTERFACE:'Interfaces',UNION:'Unions',ENUM:'Enums',ENUM_VALUE:'Enum-Werte',SCALAR:'Scalars',FIELD:'Felder',ARGUMENT:'Argumente',INPUT_FIELD:'Input-Felder',DIRECTIVE:'Direktiven'};
            const slots={QUERY_ROOT:[0,0],MUTATION_ROOT:[0,1],SUBSCRIPTION_ROOT:[0,2],QUERY_OPERATION:[1,0],MUTATION_OPERATION:[1,1],SUBSCRIPTION_OPERATION:[1,2],OBJECT:[2,0],INTERFACE:[2,1],UNION:[2,2],INPUT_OBJECT:[2,3],ENUM:[2,4],SCALAR:[2,5],FIELD:[3,0],INPUT_FIELD:[3,3],ENUM_VALUE:[3,4],ARGUMENT:[4,0],DIRECTIVE:[4,5]};
            const counts=new Map(),nodeKinds=new Map(graph.nodes.map(node=>[node.node_id,node.kind]));for(const node of graph.nodes)counts.set(node.kind,(counts.get(node.kind)||0)+1);
            const cards=new Map(),cardWidth=250,cardHeight=82,xStep=300,yStep=104;
            for(const [kind,count] of counts){const slot=slots[kind]||[4,4],x=30+slot[0]*xStep,y=30+slot[1]*yStep;cards.set(kind,{kind,count,x,y,width:cardWidth,height:cardHeight});}
            const overviewWidth=30+5*xStep,overviewHeight=30+6*yStep,fitBox=[0,0,overviewWidth,overviewHeight];box=box||[...fitBox];canvas.setAttribute('viewBox',box.join(' '));canvas.replaceChildren();
            const defs=svg('defs'),marker=svg('marker',{id:markerId,viewBox:'0 0 10 10',refX:9,refY:5,markerWidth:6,markerHeight:6,orient:'auto-start-reverse'});marker.append(svg('path',{d:'M 0 0 L 10 5 L 0 10 z','class':'ht-arrow'}));defs.append(marker);canvas.append(defs);
            const aggregate=new Map();for(const edge of graph.edges){const from=nodeKinds.get(edge.source_id),to=nodeKinds.get(edge.target_id);if(!from||!to||from===to||!cards.has(from)||!cards.has(to))continue;const key=from+'→'+to;aggregate.set(key,{from,to,count:(aggregate.get(key)?.count||0)+1});}
            for(const item of aggregate.values()){const a=cards.get(item.from),b=cards.get(item.to),forward=b.x>=a.x,x1=forward?a.x+a.width:a.x,x2=forward?b.x:b.x+b.width,y1=a.y+a.height/2,y2=b.y+b.height/2,mid=(x1+x2)/2;const path=svg('path',{d:`M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2},${y2}`,'class':'ht-edge','marker-end':`url(#${markerId})`,'stroke-width':Math.min(5,1+Math.log10(item.count+1)),'stroke-opacity':'.5'}),title=svg('title');title.textContent=`${labels[item.from]||item.from} → ${labels[item.to]||item.to}: ${item.count} Beziehungen`;path.append(title);canvas.append(path);}
            const selectedKind=selected?nodeKinds.get(selected):null;
            for(const card of cards.values()){const label=labels[card.kind]||card.kind,g=svg('g',{'data-overview-kind':card.kind,'data-kind':card.kind,'class':'ht-node'+(card.kind===selectedKind?' is-selected':''),tabindex:0,role:'button','aria-label':`${label}: ${card.count} Knoten. Gefilterte Ansicht öffnen.`});
                g.append(svg('rect',{x:card.x,y:card.y,width:card.width,height:card.height,rx:8}),svg('rect',{x:card.x+2,y:card.y+2,width:6,height:card.height-4,rx:3,'class':'ht-kind-mark'}));
                const title=svg('text',{x:card.x+14,y:card.y+28});title.textContent=label;const count=svg('text',{x:card.x+14,y:card.y+55});count.textContent=`${card.count} Knoten`;g.append(title,count);
                const open=()=>{this.graphSearch.value='';this.graphKind.value=card.kind;this.relationKind.value='';this.loadGraph();};g.addEventListener('click',open);g.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();open();}});canvas.append(g);
            }
            canvas.setAttribute('data-total-nodes',graph.nodes.length);canvas.setAttribute('data-total-edges',graph.edges.length);canvas._heroScene={mode:'overview',box,fitBox,nodes:[...cards.values()].map(card=>({x:card.x,y:card.y,width:card.width,height:card.height})),selected,notice};notice.textContent=`${this.graphFilterActive?'Gefilterte Übersicht':'Schema Overview'} nach Knotenklasse · ${graph.nodes.length} Knoten · ${graph.edges.length} Beziehungen. Knotenklasse wählen, um die vorhandene gefilterte Detailansicht zu öffnen.`;return box;
        }
        renderGraphScene(canvas,graph,selected,box,select,notice,markerId){
            if(canvas===this.canvas&&this.graphNeedsOverview(graph))return this.renderGraphOverview(canvas,graph,selected,box,notice,markerId);
            const rect=canvas.getBoundingClientRect();
            const nodes=graph.nodes,layout=this.graphLayout(graph),{positions,nodeWidth,nodeHeight}=layout,routes=[],sideLanes=new Map(),selection=this.graphSelectionState(graph,selected),nodeKinds=new Map(nodes.map(node=>[node.node_id,node.kind])),rootKinds=new Set(['QUERY_ROOT','MUTATION_ROOT','SUBSCRIPTION_ROOT']);
            for(const edge of graph.edges){const a=positions.get(edge.source_id),b=positions.get(edge.target_id);if(!a||!b)continue;const ay=a.y+nodeHeight/2,by=b.y+nodeHeight/2;let x1,y1=ay,x2,y2=by,c1x,c2x,labelX,labelY;
                if(b.x>a.x+nodeWidth*.35){x1=a.x+nodeWidth;x2=b.x;const mid=x1+(x2-x1)/2;c1x=mid;c2x=mid;labelX=mid;labelY=(ay+by)/2;}
                else if(a.x>b.x+nodeWidth*.35){x1=a.x;x2=b.x+nodeWidth;const mid=x2+(x1-x2)/2;c1x=mid;c2x=mid;labelX=mid;labelY=(ay+by)/2;}
                else {const key=Math.round(Math.max(a.x,b.x)),slot=sideLanes.get(key)||0;sideLanes.set(key,slot+1);const side=Math.max(a.x,b.x)+nodeWidth+48+slot*14;x1=a.x+nodeWidth;x2=b.x+nodeWidth;c1x=side;c2x=side;labelX=side;labelY=(ay+by)/2;}
                const bounds={x:Math.min(x1,x2,c1x,c2x),y:Math.min(y1,y2),width:Math.max(1,Math.max(x1,x2,c1x,c2x)-Math.min(x1,x2,c1x,c2x)),height:Math.max(1,Math.abs(y2-y1))};
                const edgeKey=this.edgeKey(edge),related=selection.relatedEdges.has(edgeKey),rootEdge=rootKinds.has(nodeKinds.get(edge.source_id))||rootKinds.has(nodeKinds.get(edge.target_id));
                routes.push({edge,edgeKey,related,rootEdge,x1,y1,x2,y2,c1x,c2x,labelX,labelY,bounds,d:`M${x1},${y1} C${c1x},${y1} ${c2x},${y2} ${x2},${y2}`});
            }
            const nodeRects=Array.from(positions.values(),p=>({x:p.x,y:p.y,width:nodeWidth,height:nodeHeight}));let minX=0,minY=0,maxX=layout.width,maxY=layout.height;
            for(const r of routes){minX=Math.min(minX,r.bounds.x);minY=Math.min(minY,r.bounds.y);maxX=Math.max(maxX,r.bounds.x+r.bounds.width);maxY=Math.max(maxY,r.bounds.y+r.bounds.height);}
            const fitBox=[minX-30,minY-30,Math.max(320,maxX-minX+60),Math.max(300,maxY-minY+60)];box=box||[...fitBox];
            const scale=Math.min(rect.width/box[2],rect.height/box[3]),compact=scale<.15;canvas.setAttribute('viewBox',box.join(' '));canvas.replaceChildren();
            const defs=svg('defs'),marker=svg('marker',{id:markerId,viewBox:'0 0 10 10',refX:9,refY:5,markerWidth:6,markerHeight:6,orient:'auto-start-reverse'});marker.append(svg('path',{d:'M 0 0 L 10 5 L 0 10 z','class':'ht-arrow'}));defs.append(marker);canvas.append(defs);
            const visible=(x,y,w,h)=>x+w>=box[0]&&x<=box[0]+box[2]&&y+h>=box[1]&&y<=box[1]+box[3],visibleRoutes=routes.filter(r=>visible(r.bounds.x,r.bounds.y,r.bounds.width,r.bounds.height)).sort((a,b)=>Number(a.related)-Number(b.related));
            for(const r of visibleRoutes){const classes=['ht-edge','ht-edge-detail'];if(r.rootEdge)classes.push('ht-edge-root');if(r.related)classes.push('is-related');else if(selection.active)classes.push('is-dimmed');const path=svg('path',{d:r.d,'class':classes.join(' '),'data-edge':r.edgeKey,'marker-end':`url(#${markerId})`}),title=svg('title');title.textContent=`${r.edge.source_id} — ${r.edge.kind}${r.edge.label?' ('+r.edge.label+')':''} → ${r.edge.target_id}`;path.append(title);canvas.append(path);}
            for(const node of nodes){const p=positions.get(node.node_id);if(!visible(p.x,p.y,nodeWidth,nodeHeight))continue;const classes=['ht-node'];if(rootKinds.has(node.kind))classes.push('is-root');if(node.node_id===selected)classes.push('is-selected');else if(selection.neighbors.has(node.node_id))classes.push('is-neighbor');else if(selection.active)classes.push('is-dimmed');const g=svg('g',{'data-node':node.node_id,'data-kind':node.kind,'class':classes.join(' '),tabindex:0,role:'button','aria-label':node.node_id,'aria-pressed':String(node.node_id===selected)});
                g.append(svg('rect',{x:p.x,y:p.y,width:nodeWidth,height:nodeHeight,rx:6}),svg('rect',{x:p.x+2,y:p.y+2,width:5,height:nodeHeight-4,rx:2,'class':'ht-kind-mark'}));if(!compact){for(const [i,value] of [node.name,node.kind,node.type_ref||''].entries()){const t=svg('text',{x:p.x+10,y:p.y+19+i*19,'class':['ht-node-name','ht-node-kind','ht-node-type'][i]});t.textContent=value.length>31?value.slice(0,29)+'…':value;g.append(t);}}
                g.addEventListener('click',()=>select(node.node_id));g.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();select(node.node_id);}});canvas.append(g);
            }
            // DOM culling changes visibility only; the complete projection and zoom bounds stay intact.
            canvas.setAttribute('data-total-nodes',nodes.length);canvas.setAttribute('data-total-edges',graph.edges.length);
            canvas._heroScene={mode:'detail',routes:visibleRoutes,allRoutes:routes,nodes:nodeRects,selected,selection,box,fitBox,notice,compact};this.renderGraphLabels(canvas,rect);return box;
        }
        resizeGraph(canvas){
            const scene=canvas?._heroScene,rect=canvas?.getBoundingClientRect();if(!scene||!rect?.width||!rect.height)return;if(scene.mode==='overview')return;
            const compact=Math.min(rect.width/scene.box[2],rect.height/scene.box[3])<.15;
            if(compact!==scene.compact){if(canvas===this.canvas)this.drawGraph();else if(this.focusOpen)this.drawFocus();}
            else this.renderGraphLabels(canvas,rect);
        }
        renderGraphLabels(canvas=this.canvas,rect=canvas.getBoundingClientRect()) {
            const scene=canvas?._heroScene;if(!scene||scene.mode!=='detail'||!rect.width)return;
            const scale=Math.min(rect.width/scene.box[2],rect.height/scene.box[3]),placed=[];canvas.querySelectorAll('.ht-label-bg,.ht-edge-label').forEach(n=>n.remove());
            const overlaps=(a,b,pad=0)=>a.x<b.x+b.width+pad&&a.x+a.width+pad>b.x&&a.y<b.y+b.height+pad&&a.y+a.height+pad>b.y;
            const routes=[...scene.routes].sort((a,b)=>Number(b.edge.source_id===scene.selected||b.edge.target_id===scene.selected)-Number(a.edge.source_id===scene.selected||a.edge.target_id===scene.selected));
            for(const route of routes){if(scale<.65)break;const width=Math.max(90,(route.edge.kind.length*8+16)/scale),height=20/scale,box={x:route.labelX-width/2,y:route.labelY-height/2,width,height};if(scene.nodes.some(n=>overlaps(box,n,10))||placed.some(n=>overlaps(box,n,10)))continue;
                const background=svg('rect',{...box,'class':'ht-label-bg'+(scene.selection?.active&&!route.related?' is-dimmed':'')}),label=svg('text',{x:route.labelX,y:route.labelY+4/scale,'text-anchor':'middle','class':'ht-edge-label'+(route.related?' is-related':scene.selection?.active?' is-dimmed':'')});label.style.fontSize=`${12/scale}px`;label.textContent=route.edge.kind;canvas.append(background,label);placed.push(box);
            }
            scene.notice.textContent=(scene.compact?'Übersicht ohne Knotentexte; Namen und Details über Auswahl und Inspector. ':'')+(routes.length>placed.length?`Kantenbeschriftungen reduziert: ${placed.length} von ${routes.length} sichtbaren Beziehungen beschriftet. Alle Beziehungen bleiben erhalten; Details im Knotenfokus und Inspector.`:'Alle sichtbaren Beziehungen sind beschriftet.');
        }
        applyCanvasViewbox(canvas,box,frameKey,redraw){
            // Move the existing SVG immediately; coalesce expensive culling to one frame.
            canvas.setAttribute('viewBox',box.join(' '));
            if(this[frameKey])return;this[frameKey]=requestAnimationFrame(()=>{this[frameKey]=null;redraw();});
        }
        applyViewbox(){if(this.viewbox)this.applyCanvasViewbox(this.canvas,this.viewbox,'graphFrame',()=>this.drawGraph());}
        applyFocusViewbox(){if(this.focusViewbox)this.applyCanvasViewbox(this.focusCanvas,this.focusViewbox,'focusGraphFrame',()=>this.drawFocus());}
        zoomedBox(box,scale){const [x,y,w,h]=box,nw=w*scale,nh=h*scale;return [x+(w-nw)/2,y+(h-nh)/2,nw,nh];}
        visibleNodeCount(scene,box){const [x,y,w,h]=box,eps=.5;return (scene.nodes||[]).filter(n=>n.x>=x-eps&&n.y>=y-eps&&n.x+n.width<=x+w+eps&&n.y+n.height<=y+h+eps).length;}
        boundedZoom(canvas,box,factor){const scene=canvas?._heroScene;if(!scene?.fitBox||!box||factor===1)return box;const fit=scene.fitBox;
            if(factor>1){const maxScale=Math.min(fit[2]/box[2],fit[3]/box[3]);if(maxScale<=1.000001)return [...fit];const scale=Math.min(factor,maxScale);if(scale>=maxScale-.000001)return [...fit];return this.zoomedBox(box,scale);}
            const target=Math.min(2,(scene.nodes||[]).length);if(!target)return box;const candidate=this.zoomedBox(box,factor);if(this.visibleNodeCount(scene,candidate)>=target)return candidate;if(this.visibleNodeCount(scene,box)<target)return box;
            // Find the tightest centered box that still keeps at least two complete nodes
            // (or the sole node of a one-node graph) visible.
            let low=factor,high=1;for(let i=0;i<20;i++){const mid=(low+high)/2,test=this.zoomedBox(box,mid);if(this.visibleNodeCount(scene,test)>=target)high=mid;else low=mid;}return this.zoomedBox(box,high);
        }
        zoom(factor){if(!this.viewbox)return;const next=this.boundedZoom(this.canvas,this.viewbox,factor);if(next===this.viewbox)return;this.viewbox=next;this.applyViewbox();}
        async nodeData(id,scope=this.scope(true)){
            if(this.graphCache?.key===this.scopeKey(scope)){
                const cache=this.graphCache;
                if(!cache.details){
                    // Index the existing projection once; focus partner reads never rescan
                    // every edge or synthesize relations from schema names.
                    cache.details=new Map(cache.data.nodes.map(node=>[node.node_id,{node,incoming:[],outgoing:[]}]));
                    for(const edge of cache.data.edges){cache.details.get(edge.source_id).outgoing.push(edge);cache.details.get(edge.target_id).incoming.push(edge);}
                }
                const detail=cache.details.get(id);if(detail)return detail;
            }
            return call('get_graph',{...scope,node_id:id});
        }
        detailContents(target,data,scope,navigate){
            target.replaceChildren($('h3','',data.node.name),$('p','',data.node.node_id),$('p','',data.node.description||''),$('pre','ht-data',JSON.stringify(data.node,null,2)));this.appendMark(target,data.node,scope);
            if(this.tool==='analyzer')this.appendAnalyzerService(target,data.node,scope);
            for(const [title,edges,key] of [['Incoming',data.incoming,'source_id'],['Outgoing',data.outgoing,'target_id']]){target.append($('h4','',`${title} (${edges.length})`));for(const edge of edges)target.append(button(`${edge.kind}${edge.label?' · '+edge.label:''} · ${edge[key]}`,()=>navigate(edge[key])));}
        }
        appendAnalyzerService(target,node,scope){
            this.analyzerServiceForm=null;this.analyzerCreateButton=null;
            if(!['QUERY_OPERATION','MUTATION_OPERATION'].includes(node.kind))return;
            const button= this.analyzerCreateButton = document.createElement('button');
            button.className='btn btn-default';button.type='button';button.textContent='Als GraphQL-Service speichern';
            button.disabled=true;button.title='Service drafts from analyzer operations require legacy transport state and are unavailable here.';
            const box=$('section','ht-analyzer-service');target.append(button,box);
            button.addEventListener('click',()=>this.run(()=>this.openAnalyzerService(box,node,scope)));
            this.updateActions();
        }
        async openAnalyzerService(target,node,scope){
            const serial=this.sequence,generation=this.analyzerDetailGeneration;
            const draft=await call('get_analyzer_service_draft',{...scope,schema_node_id:node.node_id});
            if(serial!==this.sequence||generation!==this.analyzerDetailGeneration||this.scopeKey(scope)!==this.scopeKey())return;
            const name=input(),query=$('textarea','form-control'),variables=$('textarea','form-control');
            name.value=draft.payload.name;query.value=draft.payload.query;query.rows=12;variables.value=JSON.stringify(draft.payload.variables,null,2);variables.rows=5;
            name.setAttribute('aria-label','Service-Name');query.setAttribute('aria-label','GraphQL-Entwurf');variables.setAttribute('aria-label','Service-Variablen (JSON)');
            const save=button('Service speichern',()=>this.run(()=>this.saveAnalyzerService()));
            this.analyzerServiceForm={target,scope,nodeId:node.node_id,draft,name,query,variables,save,saved:false};
            target.replaceChildren($('h3','',draft.operation_kind==='mutation'?'Mutation speichern':'Query speichern'),$('p','','Provenance=API-Analyzer'),
                $('p','ht-context','Speichert einen Service, ohne ihn auszuführen. Bei Objekt-Ergebnissen ist __typename vorausgewählt; ergänze die gewünschten Felder.'),
                $('p','ht-context',draft.required_variables.length?'Pflichtvariablen vor der Ausführung ergänzen: '+draft.required_variables.map(v=>v.name+' ('+v.type+')').join(', '):'Keine Pflichtvariablen.'),
                field('Service-Name',name),field('GraphQL',query),field('Variablen (JSON)',variables),save);
            this.updateActions();
        }
        async saveAnalyzerService(){
            const form=this.analyzerServiceForm;if(!form||form.saved)return;
            if(this.scopeKey(form.scope)!==this.scopeKey())throw new Error(errors.BINDING_CHANGED);
            const payload={...form.draft.payload,name:form.name.value,query:form.query.value,variables:JSON.parse(form.variables.value)};
            const result=await call('save_analyzer_service',{...form.scope,schema_node_id:form.nodeId,payload},true);
            form.saved=true;form.save.disabled=true;
            if(this.analyzerServiceForm!==form)return;
            const link=$('a','btn btn-default','In GraphQL-Services öffnen');link.href=this.serviceUrl(result.query.service_kind,result.query.query_id);
            form.target.replaceChildren($('p','',result.query.name+' gespeichert · Provenance='+result.query.provenance),link);
            this.notice.textContent='Service gespeichert. Es wurde keine HERO-Operation ausgeführt.';
        }
        async inspectNode(id,serial=this.sequence,open=true){
            const scope=this.scope(true),key=this.scopeKey(scope),generation=this.inspectionGeneration=(this.inspectionGeneration||0)+1;
            try {const data=await this.nodeData(id,scope);if(serial!==this.sequence||generation!==this.inspectionGeneration||key!==this.scopeKey())return;
                this.selectedNode=id;this.detailContents(this.nodeDetail,data,scope,next=>this.inspectNode(next));this.drawGraph();if(open)this.openFocus(id);
            } catch(error){if(serial===this.sequence&&generation===this.inspectionGeneration){this.nodeDetail.replaceChildren($('p','text-danger',error.message));}}
        }
        openFocus(id){
            if(!this.focusDialog){const dialog=new frappe.ui.Dialog({title:'HERO · Node Focus',size:'extra-large',fields:[]});dialog.$wrapper.addClass('ht-focus-dialog');const toolbar=$('div','ht-toolbar');
                this.focusBack=button('← Focus Back',()=>{if(this.focusHistory.length>1){this.focusHistory.pop();this.loadFocus(this.focusHistory.at(-1));}});
                this.focusFit=button('Fit',()=>{this.focusViewbox=null;this.drawFocus();});this.focusZoomIn=button('+',()=>this.zoomFocus(.8));this.focusZoomOut=button('−',()=>this.zoomFocus(1.25));toolbar.append(this.focusBack,this.focusFit,this.focusZoomIn,this.focusZoomOut);
                this.focusCanvas=svg('svg',{'class':'ht-graph',height:420,'aria-label':'Direct relations in node focus'});this.bindGraphNavigation(this.focusCanvas,()=>this.focusViewbox,box=>{this.focusViewbox=box;},()=>this.applyFocusViewbox(),factor=>this.zoomFocus(factor));
                this.focusScalar=$('section','ht-scalar-focus');this.focusScalar.hidden=true;this.focusScalar.setAttribute('aria-label','Verwendungen des GraphQL-Basisskalars');
                this.focusDetail=$('aside','ht-inspector');this.focusNotice=$('p','ht-legend');const grid=$('div','ht-split');grid.append(this.focusCanvas,this.focusScalar,this.focusDetail);dialog.body.append(toolbar,this.focusNotice,grid);dialog.set_secondary_action(()=>dialog.hide());dialog.set_secondary_action_label('Close');
                dialog.onhide=()=>{this.focusGeneration=(this.focusGeneration||0)+1;this.focusOpen=false;};dialog.$wrapper.on('hidden.bs.modal.heroFocus',()=>{const node=Array.from(this.canvas.querySelectorAll('[data-node]')).find(n=>n.dataset.node===this.selectedNode);(node||this.focusReturn)?.focus({preventScroll:true});});this.focusDialog=dialog;this.resize.observe(this.focusCanvas);
            }
            this.focusReturn=document.activeElement;this.focusHistory=[id];this.focusOpen=true;this.focusDialog.show();this.loadFocus(id);
        }
        async loadFocus(id){
            const serial=this.sequence,scope=this.scope(true),key=this.scopeKey(scope),generation=this.focusGeneration=(this.focusGeneration||0)+1;
            const current=()=>this.focusOpen&&serial===this.sequence&&generation===this.focusGeneration&&key===this.scopeKey();
            this.focusCanvas.replaceChildren();this.focusCanvas._heroScene=null;this.focusScalar.replaceChildren();this.focusDetail.replaceChildren();this.focusNotice.textContent='Loading direct relations …';this.focusBack.disabled=this.focusHistory.length<2;
            try {const detail=await this.nodeData(id,scope);if(!current())return;this.focusNode=id;this.focusViewbox=null;
                if(this.isBuiltinScalar(detail.node)){this.focusGraph=null;this.setFocusGraphMode(false);this.scalarDetailContents(this.focusDetail,detail,scope);this.renderScalarFocus(detail);return;}
                this.setFocusGraphMode(true);const edges=new Map([...detail.incoming,...detail.outgoing].map(e=>[this.edgeKey(e),e])),ids=new Set([id]);for(const e of edges.values()){ids.add(e.source_id);ids.add(e.target_id);}const nodes=[];
                for(const other of ids){const n=other===id?detail.node:(await this.nodeData(other,scope)).node;if(!current())return;nodes.push(n);}
                this.focusGraph={nodes,edges:[...edges.values()]};this.detailContents(this.focusDetail,detail,scope,next=>this.focusNext(next));this.drawFocus();
            } catch(error){if(current())this.focusNotice.textContent='Fokus konnte nicht geladen werden: '+error.message;}
        }
        focusNext(id){if(id!==this.focusHistory.at(-1))this.focusHistory.push(id);this.loadFocus(id);}
        drawFocus(){if(this.focusGraph)this.focusViewbox=this.renderGraphScene(this.focusCanvas,this.focusGraph,this.focusNode,this.focusViewbox,id=>this.focusNext(id),this.focusNotice,'hero-focus-arrow');}
        zoomFocus(factor){if(!this.focusViewbox)return;const next=this.boundedZoom(this.focusCanvas,this.focusViewbox,factor);if(next===this.focusViewbox)return;this.focusViewbox=next;this.applyFocusViewbox();}
        appendMark(target,node,scope){
            const group=$('div','ht-mark-action'),status=$('p','ht-mark-status');status.setAttribute('role','status');const mark=button('Mark for Mapping',async()=>{
                if(mark.disabled)return;mark.disabled=true;const key=this.scopeKey(scope);status.textContent='Checking mapping candidate …';
                try {const rows=await this.candidateRecords(scope);if(key!==this.scopeKey())throw new Error('Kontext geändert. Element erneut auswählen.');const old=rows.find(c=>c.schema_node_id===node.node_id);
                    const result=await call('mark_candidate',{...scope,node_id:node.node_id,expected_revision:old?.revision||0},true);
                    status.textContent=`Vorgemerkt · Revision ${result.candidate.revision} · Vorschlag, kein Core-Mapping.`;if(key===this.scopeKey()&&this.tool==='graph'){try{await this.loadCandidates();}catch(error){status.textContent+=' Die Vormerkungsliste konnte nicht aktualisiert werden: '+error.message;}}
                } catch(error){status.textContent=error.message;}finally{mark.disabled=!this.context?.can_write||key!==this.scopeKey();}
            });mark.classList.add('ht-mark-primary');
            // The existing server permits every valid graph node; there is no additional
            // kind whitelist. Do not invent stricter semantic mapping eligibility here.
            const validIdentity=[node.node_id.split(':').slice(1).join(':'),node.name].every(v=>v&&!['/','\\','..'].some(s=>v.includes(s)));
            const reason=!this.context?.can_write?'No write permission for mapping candidates.':this.context.needs_binding_update?'Apply the connection first.':!validIdentity?'This element does not have a valid identity for the mapping-candidate contract.':'Use the native Mapping Editor for reviewed mapping proposals.';
            mark.disabled=!!reason;status.textContent=reason||'HERO source candidate · does not automatically create or activate a mapping.';group.append(mark,status);target.append(group);
        }
        async candidateRecords(scope){return [];}
        async loadCandidates(serial=this.sequence) {
            if(serial!==this.sequence||!this.schema)return;const scope=this.scope(true),key=this.scopeKey(scope),rows=await this.candidateRecords(scope);if(serial!==this.sequence||key!==this.scopeKey())return;this.candidateRows=rows;
            this.candidates.replaceChildren($('h3','','Mapping Candidates'),$('p','','Candidates do not create or activate a mapping.'));
            this.candidates.append($('p','ht-context','Reviewed mapping proposals are managed in the native Mapping Editor.'));
            const link=$('a','','Open Existing Variable Mapping');link.href=hero_integration_shell.mapping('mappings','HERO');this.candidates.append(hero_integration_shell.navigationLink(link));
        }
        serviceLabel() { return this.serviceKind === 'mutation' ? 'Mutation' : 'Query'; }
        serviceScope(schema=false) { return {...this.scope(schema), operation_kind:this.serviceKind}; }
        serviceUrl(kind, queryId='') {
            const platform=window.hero_integration_platform;
            const target=platform ? new URL(platform.url('connections/hero/graphql-services/'+kind), location.origin) : new URL(location.href);
            if(!platform){target.searchParams.set('tool','services');target.searchParams.set('operation_kind',kind);}
            target.searchParams.delete('query');
            if(queryId)target.searchParams.set('query',queryId);
            return target.pathname+target.search+target.hash;
        }
        buildServices() {
            const p=this.panels.services,grid=$('div','ht-split');this.queryList=$('div','ht-list');const editor=$('section','ht-query-editor');this.name=input();this.category=input();this.category.value=this.serviceKind==='mutation'?'Custom Mutations':'Custom Queries';this.operation=input();this.description=$('textarea','form-control');
            const views=$('nav','ht-toolbar');views.setAttribute('aria-label','GraphQL Services');
            for(const [kind,label] of [['query','Query'],['mutation','Mutation']]) {
                const link=$('a','btn btn-default',label);link.href=this.serviceUrl(kind);
                if(kind===this.serviceKind)link.setAttribute('aria-current','page');
                if(kind==='mutation'){link.setAttribute('aria-disabled','true');link.title='HERO mutations are disabled by the native security boundary.';link.addEventListener('click',event=>event.preventDefault());}
                link.addEventListener('click',e=>{if(this.busy||!this.confirmReplace())e.preventDefault();});views.append(link);
            }
            this.editorHost=$('div','ht-editor');this.variables=$('textarea','form-control ht-variables');this.variables.value='{}';this.queryOutput=$('pre','ht-data');this.queryOutput.setAttribute('aria-live','polite');this.queryStatus=$('p','ht-query-status');this.queryStatus.setAttribute('role','status');
            const bar=$('div','ht-toolbar');this.newButton=button('New '+this.serviceLabel(),()=>this.newQuery());this.copyButton=button('Duplicate',()=>this.run(async()=>this.newQuery(true)));this.saveButton=button('Save',()=>this.run(()=>this.saveQuery()));this.validateButton=button('Validate',()=>this.run(()=>this.validateQuery()));this.executeButton=button('Execute Saved '+this.serviceLabel(),()=>this.run(()=>this.executeQuery()));this.deleteButton=button('Delete',()=>this.run(()=>this.deleteQuery()));bar.append(this.newButton,this.copyButton,this.saveButton,this.validateButton,this.executeButton,this.deleteButton);
            this.serviceHint=$('p','ht-service-hint');this.queryProvenance=$('p','ht-query-provenance');
            editor.append(this.queryStatus,this.queryProvenance,field('Name',this.name),field('Category',this.category),field('Description',this.description),field('operationName',this.operation),$('h3','','GraphQL'),this.editorHost,field('Variables (JSON)',this.variables),bar,this.queryOutput);grid.append(this.queryList,editor);this.queryPager=this.pager('services',()=>this.loadQueries());p.append(views,$('h2','','GraphQL Services · '+this.serviceLabel()),this.serviceHint,grid,this.queryPager.root);
            for(const n of [this.name,this.category,this.description,this.operation,this.variables])n.addEventListener('input',()=>this.queryChanged());
        }
        async initializeEditor(){
            if(window.hero_integration_platform?.loadAce){
                await window.hero_integration_platform.loadAce();
            }else if(window.frappe?.ui?.form?.ControlCode?.prototype?.load_lib){
                // Legacy Frappe Desk compatibility.
                await frappe.ui.form.ControlCode.prototype.load_lib.call({});
            }else if(!window.ace){
                throw new Error('Ace editor loader is not available.');
            }

            this.editor=window.ace.edit(this.editorHost);
            this.editor.setOptions({
                fontSize:14,
                showPrintMargin:false,
                tabSize:2,
                useSoftTabs:true
            });
            this.editor.session.setUseWorker(false);
            this.editor.session.on(
                'change',
                ()=>this.queryChanged()
            );
            this.base=this.draftString();
        }
        queryChanged(){this.validation=null;this.queryOutput.textContent='';this.updateActions();}
        queryString(q, variables) {
            const canonical=value=>Array.isArray(value)?value.map(canonical):value&&typeof value==='object'?Object.fromEntries(Object.keys(value).sort().map(key=>[key,canonical(value[key])])):value;
            // Match SavedQuery's text normalization; JSON whitespace/key order is not a change.
            return JSON.stringify([(q.name||'').trim(),(q.category||'').trim()||(this.serviceKind==='mutation'?'Custom Mutations':'Custom Queries'),(q.description||'').trim(),(q.operation_name||'').trim(),(q.query||'').trim(),variables||['json',canonical(q.variables===undefined?{}:q.variables)]]);
        }
        draftString(){let variables;try{variables=JSON.parse(this.variables.value);}catch(e){return this.queryString({name:this.name.value,category:this.category.value,description:this.description.value,operation_name:this.operation.value,query:this.editor?.getValue()||''},['invalid-json',this.variables.value]);}return this.queryString({name:this.name.value,category:this.category.value,description:this.description.value,operation_name:this.operation.value,query:this.editor?.getValue()||'',variables});}
        get dirty(){return this.base!==undefined&&this.base!==this.draftString();}
        confirmReplace(){return !this.dirty||window.confirm('Discard the unsaved service draft?');}
        payload(){return {name:this.name.value,category:this.category.value.trim()||(this.serviceKind==='mutation'?'Custom Mutations':'Custom Queries'),description:this.description.value,operation_name:this.operation.value||null,query:this.editor.getValue(),variables:JSON.parse(this.variables.value)};}
        setQuery(q={}){this.copiedFrom=q.copied_from||'';this.draftProvenance=q.provenance||'GraphQL-Services';this.queryEpoch=(this.queryEpoch||0)+1;this.query=q.query_id?q:null;this.name.value=q.name||'';this.category.value=q.category||(this.serviceKind==='mutation'?'Custom Mutations':'Custom Queries');this.description.value=q.description||'';this.operation.value=q.operation_name||'';this.editor.setValue(q.query||'',-1);this.variables.value=JSON.stringify(q.variables||{},null,2);this.base=this.draftString();this.validation=null;this.queryOutput.textContent=q.query_id?`${this.serviceLabel()} ${q.query_id} · Revision ${q.revision}`:'New unsaved draft';this.updateActions();}
        newQuery(copy=false){if(!copy&&!this.confirmReplace())return;const q=copy?{...this.payload(),name:this.name.value+' (Copy)',copied_from:this.query?.query_id||this.copiedFrom,provenance:this.query?.provenance||this.draftProvenance}:{};this.setQuery(q);if(copy)this.base='';}
        async loadQueries(serial=this.sequence){const data=await call('list_services',{...this.serviceScope(),start:this.starts.services,page_size:50});if(serial!==this.sequence)return;this.queryList.replaceChildren($('h3','',this.serviceKind==='mutation'?'Saved Mutations':'Saved Queries'));for(const q of data.rows)this.queryList.append(button(`${q.name} · r${q.revision} · Provenance=${q.provenance||'Legacy (unbekannt)'}`,()=>{if(!this.busy&&this.confirmReplace())this.setQuery(q);}));this.queryPager.update(data.has_more);
            const queryId=new URLSearchParams(location.search).get('query');
            if(queryId && queryId!==this.loadedQueryId && queryId!==this.query?.query_id && !this.dirty) {const result=await call('get_query',{...this.scope(),query_id:queryId});if(serial===this.sequence){if(result.query.service_kind!==this.serviceKind){location.assign(this.serviceUrl(result.query.service_kind,queryId));return;}this.setQuery(result.query);this.loadedQueryId=queryId;}}
        }
        async saveQuery(){
            const scope=this.scope(),epoch=this.queryEpoch,queryId=this.query?.query_id||'',revision=this.query?.revision||0;
            this.validation=null;
            const data=await call('save_query',{...scope,operation_kind:this.serviceKind,payload:this.payload(),query_id:queryId,expected_revision:revision,copied_from:queryId?'':this.copiedFrom||''},true);
            if(epoch!==this.queryEpoch||queryId!==(this.query?.query_id||'')||revision!==(this.query?.revision||0)||scope.connection_id!==this.context?.connection_id||scope.binding_generation!==this.context?.binding_generation)return;
            // The acknowledgement defines the saved base, even if the local buffer has changed.
            // Never replace the live Ace buffer or form fields with the response.
            this.query=data.query;this.draftProvenance=data.query.provenance;this.base=this.queryString(data.query);this.validation=null;
            this.queryOutput.textContent=`Saved · Revision ${data.query.revision}${this.dirty?' · Additional local changes are not saved.':''}`;
            await this.loadQueries();
        }
        async validateQuery(){const stamp=this.draftString();const data=this.query&&!this.dirty?await call('validate_saved_query',{...this.serviceScope(true),query_id:this.query.query_id,expected_revision:this.query.revision},true):await call('preview_query',{...this.serviceScope(true),payload:this.payload()},true);if(stamp!==this.draftString())return;this.validation=data;this.queryOutput.textContent=JSON.stringify(data,null,2);}
        async executeQuery(){
            if(this.dirty||!this.query||!this.validation?.can_execute)throw new Error('Save and validate the current service first.');
            const mutation=this.serviceKind==='mutation';
            if(mutation&&!window.confirm(`Mutation "${this.query.name}" · Revision ${this.query.revision} mit den angezeigten Variablen auf HERO ausführen? Dies schreibt Daten.`))return;
            const data=await call('execute_service',{...this.serviceScope(true),query_id:this.query.query_id,expected_revision:this.query.revision,variables:JSON.parse(this.variables.value),mutation_confirmed:mutation},true);
            this.queryOutput.textContent=JSON.stringify(data,null,2);
        }
        async deleteQuery(){if(!this.query||!window.confirm(this.dirty?'Delete the saved service and discard the unsaved draft?':'Delete the saved service?'))return;await call('delete_query',{...this.serviceScope(),query_id:this.query.query_id,expected_revision:this.query.revision},true);this.setQuery();await this.loadQueries();}
        updateActions(){if(!this.saveButton)return;
            const analyzerAllowed=!!this.context?.can_write&&!this.context.needs_binding_update&&!this.busy;
            if(this.analyzerCreateButton)this.analyzerCreateButton.disabled=!analyzerAllowed||!!this.analyzerServiceForm;
            if(this.analyzerServiceForm)this.analyzerServiceForm.save.disabled=!analyzerAllowed||this.analyzerServiceForm.saved;
            if(this.queryProvenance)this.queryProvenance.textContent='Provenance='+(this.query?.provenance||this.draftProvenance||'GraphQL-Services');
            if(this.serviceHint)this.serviceHint.textContent=this.serviceKind==='mutation'
                ? 'Gespeicherte Mutations schreiben Daten auf HERO. Speichern und Prüfen bleiben unabhängig vom Mutationsschalter möglich. '+(this.context?.mutations_enabled?'Mutationen aktiviert. Jede Ausführung benötigt eine Bestätigung.':'Mutationen deaktiviert: Ausführung gesperrt.')
                : 'Gespeicherte Queries lesen Daten aus HERO. Ergebnisse werden nicht automatisch gespeichert.';
this.queryStatus.textContent=this.query?`${this.serviceLabel()} ${this.query.query_id} · Revision ${this.query.revision}${this.dirty?' · Unsaved changes':''}`:'New unsaved draft';const allowed=!!this.context?.can_write&&!this.context.needs_binding_update&&!this.busy;for(const b of [this.newButton,this.copyButton,this.saveButton])b.disabled=!allowed;this.validateButton.disabled=!allowed||!this.schema;this.executeButton.disabled=true;this.executeButton.title='Remote execution is disabled in the native workspace.';this.deleteButton.disabled=!allowed||!this.query;this.bindButton.disabled=!this.context?.can_write||this.busy;}
        destroy(){
            this.cancelGraph?.();
            this.focusDialog?.hide?.();
            this.resize?.disconnect?.();
            this.editor?.destroy?.();
            this.editor=null;
            if(this.beforeUnloadHandler){
                window.removeEventListener('beforeunload',this.beforeUnloadHandler);
            }
            this.root?.remove?.();
        }
    }
    window.hero_workspace_tools={Workspace};
})();
