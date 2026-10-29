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
    const errors = {CONFIGURATION_REQUIRED:'The HERO connection is not configured completely.', PERMISSION_DENIED:'You do not have permission to use this tool action.',
        BINDING_CHANGED:'The connection changed. Reload the context and accept the change deliberately.', REVISION_CONFLICT:'The record changed in the meantime. Reload it; your draft is retained.',
        SCHEMA_CHANGED:'The current schema changed. Validate again.', SCHEMA_NOT_FOUND:'No schema is available for this connection and revision.',
        SCHEMA_ANALYSIS_FAILED:'The complete schema fetch failed. No replacement schema was created.', READ_ONLY_BLOCKED:'This operation cannot be run here.',
        SERVICE_KIND_MISMATCH:'This operation belongs in the other service view (Query or Mutation).',
        INVALID_QUERY:'The GraphQL text does not match the saved schema.', ANALYZER_OPERATION_REQUIRED:'Select a Query or Mutation operation.',
        INVALID_SERVICE_KIND:'Select Query or Mutation.', SUBSCRIPTION_NOT_SUPPORTED:'Subscriptions are not supported as services.',
        HERO_MUTATIONS_DISABLED:'HERO mutations are disabled in the connection settings.', MUTATION_APPROVAL_REQUIRED:'The exact mutation must be confirmed before execution.',
        CONNECTOR_DISABLED:'The HERO connection is disabled.', TOOL_INTERNAL_ERROR:'The tool action could not be completed on the server.'};
    /*
     * GraphQL text helpers. Pure functions without DOM access, so they can be tested in isolation.
     * Variables use one `name := value` line each; every value is parsed with JSON.parse only (never eval).
     */
    const GRAPHQL_NAME = /^[_A-Za-z][_0-9A-Za-z]*$/;
    function checkJsonNumbers(value, where) {
        if (typeof value === 'number') {
            if (!Number.isFinite(value)) throw new Error(`${where}: the number is out of range.`);
            if (Number.isInteger(value) && !Number.isSafeInteger(value)) throw new Error(`${where}: the integer is too large to keep exactly; write it as a string in double quotes.`);
        } else if (Array.isArray(value)) value.forEach((item) => checkJsonNumbers(item, where));
        else if (value && typeof value === 'object') Object.values(value).forEach((item) => checkJsonNumbers(item, where));
    }
    function parseVariablesText(text) {
        const source = String(text ?? '');
        const trimmed = source.trim();
        if (!trimmed) return {};
        if (trimmed.startsWith('{')) {
            // Compatibility: an existing JSON variables object is still accepted as input.
            let value;
            try { value = JSON.parse(trimmed); } catch (error) { throw new Error('Variables: the JSON object is invalid: ' + error.message); }
            if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Variables: JSON input must be an object.');
            for (const name of Object.keys(value)) if (!GRAPHQL_NAME.test(name)) throw new Error(`Variables: "${name}" is not a valid GraphQL variable name.`);
            checkJsonNumbers(value, 'Variables');
            return value;
        }
        const entries = [], seen = new Set(), problems = [];
        source.split(/\r?\n/).forEach((line, index) => {
            if (!line.trim()) return;
            const where = `Line ${index + 1}`, at = line.indexOf(':=');
            if (at < 0) { problems.push(`${where}: expected "name := value".`); return; }
            const name = line.slice(0, at).trim(), raw = line.slice(at + 2).trim();
            if (name.startsWith('$')) { problems.push(`${where}: write the variable name without "$".`); return; }
            if (!GRAPHQL_NAME.test(name)) { problems.push(`${where}: "${name}" is not a valid GraphQL variable name.`); return; }
            if (seen.has(name)) { problems.push(`${where}: variable "${name}" is defined more than once.`); return; }
            seen.add(name);
            if (!raw) { problems.push(`${where}: "${name}" has no value.`); return; }
            let value;
            try { value = JSON.parse(raw); } catch (error) {
                problems.push(`${where}: invalid value for "${name}". Strings need double quotes; numbers, true, false and null are written without quotes; arrays and objects use JSON.`);
                return;
            }
            try { checkJsonNumbers(value, where); } catch (error) { problems.push(error.message); return; }
            entries.push([name, value]);
        });
        if (problems.length) throw new Error(problems.join('\n'));
        // fromEntries creates own data properties, also for unusual names such as "__proto__".
        return Object.fromEntries(entries);
    }
    function formatVariablesText(variables) {
        if (!variables || typeof variables !== 'object' || Array.isArray(variables)) return '';
        return Object.entries(variables).map(([name, value]) => `${name} := ${JSON.stringify(value)}`).join('\n');
    }
    function resultKey(path, key) {
        if (GRAPHQL_NAME.test(key)) return path ? `${path}.${key}` : key;
        return `${path}[${JSON.stringify(key)}]`;
    }
    function readableResultLines(value, path = '', lines = []) {
        const label = path || '(root)';
        if (Array.isArray(value)) {
            if (!value.length) { lines.push(`${label} = []`); return lines; }
            value.forEach((item, index) => {
                // A blank line separates structured list items; the order of the list is kept.
                if (index && item && typeof item === 'object') lines.push('');
                readableResultLines(item, `${path}[${index}]`, lines);
            });
            return lines;
        }
        if (value && typeof value === 'object') {
            const keys = Object.keys(value);
            if (!keys.length) { lines.push(`${label} = {}`); return lines; }
            for (const key of keys) readableResultLines(value[key], resultKey(path, key), lines);
            return lines;
        }
        // JSON.stringify keeps the delivered type: strings get double quotes and escapes, numbers, booleans and null do not.
        lines.push(`${label} = ${value === undefined ? 'null' : JSON.stringify(value)}`);
        return lines;
    }
    function readableResult(value) { return readableResultLines(value).join('\n'); }
    function resultSummary(data) {
        if (!data || typeof data !== 'object' || Array.isArray(data)) return [];
        return Object.entries(data).map(([key, value]) => `${key}: ${Array.isArray(value) ? `${value.length} ${value.length === 1 ? 'item' : 'items'}` : value === null ? 'null' : typeof value === 'object' ? 'object' : 'value'}`);
    }
    async function call(method, args = {}, write = false, signal) {
        return window.hero_integration_rpc.call(method, args, {write, signal});
    }
    class Workspace {
        constructor(host, options = {}) {
            this.host = host; this.options = options || {}; this.serviceKind = this.options.operationKind === 'mutation' ? 'mutation' : 'query'; this.root = $('section', 'hero-tools'); this.sequence = 0; this.starts = {analyzer:0,graph:0,services:0}; this.viewbox = null;
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
        async run(action) {if(this.busy)return;this.busy=true;this.updateActions();this.notice.classList.remove('text-danger');try{this.notice.textContent='Loading …';await action();if(this.notice.textContent==='Loading …')this.notice.textContent='';}catch(e){this.notice.textContent=e.message;this.notice.classList.add('text-danger');if(this.tool==='services'){this.validation=null;this.queryOutput.textContent=e.message;}}finally{this.busy=false;this.updateActions();}}
        scope(schema=false) {return {connection_id:this.context.connection_id,binding_generation:this.context.binding_generation,...(schema?{schema_fingerprint:this.schema}: {})};}
        snapshotLabel(snapshot){const coverage=snapshot.coverage||{},counts=Number.isInteger(coverage.types)?`${coverage.types} types · ${coverage.nodes} nodes`:'no valid graph';return `${snapshot.kind||'Saved snapshot'} · ${snapshot.name} · ${counts}`;}
        updateSnapshotSelector(coverage){
            if(!this.snapshotPicker)return;
            const snapshots=Array.isArray(this.context?.snapshots)?this.context.snapshots:[];
            this.snapshotPicker.replaceChildren();
            for(const snapshot of snapshots){const option=$('option','',this.snapshotLabel(snapshot));option.value=snapshot.name;option.disabled=!snapshot.usable;this.snapshotPicker.append(option);}
            this.snapshotPicker.value=this.context?.connection_id||'';
            this.snapshotPicker.disabled=!snapshots.some(snapshot=>snapshot.usable);
            const selected=this.context?.selected_snapshot_metadata,selectedCoverage=coverage||selected?.coverage||{};
            if(!selected){this.snapshotMeta.textContent='No validated saved GraphQL snapshot is available.';return;}
            const counts=Number.isInteger(selectedCoverage.types)?`${selectedCoverage.types} types · ${selectedCoverage.nodes} nodes · ${selectedCoverage.edges} relations`:'graph analysis unavailable';
            this.snapshotMeta.textContent=`${selected.kind} · Snapshot ${selected.name} · ${selected.modified||'date unavailable'} · ${selected.schema_fingerprint} · ${counts}`;
        }
        selectSnapshot(name){
            const snapshot=(this.context?.snapshots||[]).find(item=>item.name===name);
            if(!snapshot?.usable||name===this.context?.connection_id)return;
            this.options.snapshot_name=name;
            const target=new URL(location.href);target.searchParams.set('snapshot',name);target.searchParams.delete('schema');history.replaceState(null,'',target.pathname+target.search+target.hash);
            this.show(true);
        }
        async show(force=false) {
            this.host.append(this.root);await this.ready;const serial=++this.sequence;this.cancelGraph();this.focusDialog?.hide();
            const params=new URLSearchParams(location.search), requested=params.get('hero_tool') || this.options.tool || params.get('tool') || 'analyzer';
            this.tool=Object.hasOwn(this.panels,requested)?requested:'analyzer';this.root.classList.toggle('is-analyzer',this.tool==='analyzer');
            if(this.tool==='services'&&!this.editor){
                this.editorInit=this.editorInit||this.initializeEditor();
                this.ready=this.editorInit;
                await this.ready;
            }
            const oldConnection=this.context?`${this.context.connection_id}:${this.context.binding_generation}`:'';
            const oldScope=this.context ? `${this.context.connection_id}:${this.context.binding_generation}:${this.schema}` : '';
            const requestedSnapshot=this.options.snapshot_name||params.get('snapshot')||'';
            let context=await call('get_context',{snapshot_name:requestedSnapshot});
            if(!requestedSnapshot&&params.get('schema')){
                const legacySnapshot=(context.snapshots||[]).find(item=>item.usable&&item.schema_fingerprint===params.get('schema'));
                if(legacySnapshot)context=await call('get_context',{snapshot_name:legacySnapshot.name});
            }
            if(serial!==this.sequence)return;this.context=context;
            if(oldConnection!==`${this.context.connection_id}:${this.context.binding_generation}`)this.queryEpoch=(this.queryEpoch||0)+1;
            this.options.snapshot_name=this.context.connection_id||'';
            this.schema=this.context.schema_fingerprint;
            if(oldScope!==`${this.context.connection_id}:${this.context.binding_generation}:${this.schema}`){
                this.validation=null;this.graphCache=null;this.graph=null;this.selectedNode=null;this.viewbox=null;
                this.canvas.replaceChildren();this.canvas._heroScene=null;this.typeDetail.replaceChildren();this.nodeDetail.replaceChildren();
            }
            this.status.textContent=`Source HERO · Connection ${this.context.connection_id || 'not bound'} · Binding ${this.context.binding_generation} · Schema ${this.schema || 'not available'}`;
            this.updateSnapshotSelector();
            this.bindButton.hidden=!this.context.needs_binding_update;this.refreshButton.disabled=!this.context.can_write || !this.context.enabled || this.context.needs_binding_update;
            this.tabs.replaceChildren();for(const [key,label] of [['analyzer','API Analyzer'],['graph','Graph Viewer'],['services','GraphQL Services']]) {
                const link=$('a','pc-shell-link'+(key===this.tool?' is-current':''),label);
                link.href=window.hero_integration_platform.url("api", {hero_tool: key, snapshot: this.context.connection_id || this.options.snapshot_name || ""});
                this.tabs.append(hero_integration_shell.navigationLink(link));
            }
            for(const [key,panel] of Object.entries(this.panels))panel.hidden=key!==this.tool;
            this.editor?.setReadOnly(!this.context.can_write);this.updateActions();
            if(this.context.needs_binding_update){this.notice.textContent='Configuration changed or is new. Apply the connection before using the tools.';return;}
            if(!this.schema && this.tool!=='services'){this.notice.textContent='No schema is available for this connection. Refresh Schema retrieves the defined introspection only.';return;}
            if(!this.schema && this.tool==='services'){
                this.notice.textContent='No saved GraphQL schema is available. Query and Mutation drafts remain disabled until a validated schema is selected.';
                this.queryList.replaceChildren($('p','ht-context','No saved services are available without a validated schema.'));
                this.queryOutput.textContent='Select or save a validated schema before creating a service draft.';
                this.updateActions();
                return;
            }
            this.notice.textContent='';await this.loadTool(serial);this.editor?.resize();
        }
        async loadTool(serial=this.sequence) {
            if(this.tool==='analyzer')await this.loadAnalyzer(serial);
            if(this.tool==='graph')await this.loadGraph(serial);
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
            const directiveBox=$('details','ht-analyzer-meta-card'),directiveSummary=$('summary','',`Directives (${directives.length})`),directiveList=$('div','ht-analyzer-directives');directiveBox.append(directiveSummary);if(directives.length){for(const directive of directives){const name=typeof directive==='string'?directive:directive?.name||directive?.directive||JSON.stringify(directive);directiveList.append($('span','ht-analyzer-directive',name));}}else directiveList.append($('span','ht-context','No directives are present in the saved schema.'));directiveBox.append(directiveList);
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
            const directionLabel={incoming:'Incoming',outgoing:'Outgoing'},compare=(a,b)=>a.localeCompare(b,'en',{numeric:true,sensitivity:'base'});
            return [...groups.values()].sort((a,b)=>compare(a.kind,b.kind)||compare(directionLabel[a.direction],directionLabel[b.direction])).map(group=>({...group,rows:group.rows.sort((a,b)=>compare(a.label,b.label)||compare(a.partnerId,b.partnerId))}));
        }
        setFocusGraphMode(graphMode){
            this.focusCanvas.hidden=!graphMode;this.focusScalar.hidden=graphMode;
            for(const control of [this.focusFit,this.focusZoomIn,this.focusZoomOut])control.hidden=!graphMode;
        }
        scalarDetailContents(target,detail,scope){
            const groups=this.scalarUsageRows(detail),total=groups.reduce((sum,group)=>sum+group.rows.length,0);
            target.replaceChildren($('h3','',detail.node.name),$('p','',detail.node.node_id),$('p','',detail.node.description||''),$('pre','ht-data',JSON.stringify(detail.node,null,2)));
            target.append($('h4','',`Usages (${total})`));
            for(const group of groups)target.append($('p','ht-scalar-count',`${group.kind} · ${group.direction} · ${group.rows.length}`));
        }
        renderScalarFocus(detail){
            const groups=this.scalarUsageRows(detail),total=groups.reduce((sum,group)=>sum+group.rows.length,0),root=$('div','ht-scalar-focus-content');
            const heading=$('div','ht-scalar-heading');heading.append($('div','ht-scalar-badge','SCALAR'),$('h3','',detail.node.name),$('p','ht-context',`${total} direct usages`));root.append(heading);
            root.append($('p','ht-scalar-explainer','GraphQL base scalar: usages are shown as a list instead of a relationship graph.'));
            const search=input('Filter usages');const searchField=field('Filter usages',search),status=$('p','ht-context',`${total} usages`);root.append(searchField,status);
            const groupNodes=[];
            for(const group of groups){const details=$('details','ht-scalar-group'),summary=$('summary','',`${group.kind} · ${group.direction==='incoming'?'Incoming':'Outgoing'} (${group.rows.length})`),body=$('div','ht-scalar-usage-list');details.append(summary,body);
                const rowNodes=[];for(const row of group.rows){const meta=row.node?`${row.node.kind}${row.edge.label?' · '+row.edge.label:''}`:`${row.edge.label||row.partnerId}`,b=button(`${row.label} · ${meta}`,()=>this.focusNext(row.partnerId));b.classList.add('ht-scalar-usage');b.dataset.search=`${row.label} ${row.partnerId} ${row.node?.kind||''} ${row.edge.kind} ${row.edge.label||''}`.toLocaleLowerCase();body.append(b);rowNodes.push(b);}
                root.append(details);groupNodes.push({details,rowNodes});
            }
            search.addEventListener('input',()=>{const q=search.value.trim().toLocaleLowerCase();let visible=0;for(const group of groupNodes){let groupVisible=0;for(const row of group.rowNodes){const show=!q||row.dataset.search.includes(q);row.hidden=!show;if(show){visible++;groupVisible++;}}group.details.hidden=!groupVisible;if(q&&groupVisible)group.details.open=true;}status.textContent=`${visible} of ${total} usages`;});
            this.focusScalar.replaceChildren(root);this.focusNotice.textContent=`${detail.node.name} · ${total} direct usages · list view for GraphQL base scalar`;
        }
        buildGraph() {
            const panel=this.panels.graph,bar=$('div','ht-toolbar');this.snapshotPicker=$('select','form-control');this.snapshotPicker.addEventListener('change',()=>this.selectSnapshot(this.snapshotPicker.value));this.snapshotMeta=$('p','ht-snapshot-meta');this.graphSearch=input('Name, ID or type');this.graphKind=$('select','form-control');this.relationKind=$('select','form-control');
            for(const value of ['','QUERY_OPERATION','MUTATION_OPERATION','SUBSCRIPTION_OPERATION','QUERY_ROOT','MUTATION_ROOT','SUBSCRIPTION_ROOT','OBJECT','INPUT_OBJECT','INTERFACE','UNION','ENUM','ENUM_VALUE','SCALAR','FIELD','ARGUMENT','INPUT_FIELD','DIRECTIVE']){const o=$('option','',value||'All nodes');o.value=value;this.graphKind.append(o);}
            for(const value of ['','RETURNS','HAS_FIELD','HAS_ARGUMENT','ARGUMENT_TYPE','INPUT_TYPE','IMPLEMENTS','POSSIBLE_TYPE','ROOT_TYPE','ROOT_HAS_OPERATION','HAS_INPUT_FIELD','HAS_ENUM_VALUE']){const o=$('option','',value||'All relations');o.value=value;this.relationKind.append(o);}
            const filtered=()=>this.loadGraph(),all=()=>{this.graphSearch.value='';this.graphKind.value='';this.relationKind.value='';this.loadGraph();};
            this.graphSearch.addEventListener('input',()=>{this.cancelGraph();this.graphInfo.textContent='Filter changed. Filtered View loads the current selection.';});
            for(const control of [this.graphKind,this.relationKind])control.addEventListener('change',filtered);
            bar.append(field('Snapshot',this.snapshotPicker),button('Schema Overview',all),field('Graph Search',this.graphSearch),field('Node Class',this.graphKind),field('Relation',this.relationKind),button('Filtered View',filtered),button('Fit',()=>{this.viewbox=null;this.drawGraph();}),button('+',()=>this.zoom(.8)),button('−',()=>this.zoom(1.25)));
            this.graphInfo=$('p');this.graphInfo.setAttribute('role','status');this.canvas=svg('svg',{'class':'ht-graph',height:650,'aria-label':'HERO API Graph'});
            this.bindGraphNavigation(this.canvas,()=>this.viewbox,box=>{this.viewbox=box;},()=>this.applyViewbox(),factor=>this.zoom(factor));
            this.nodeDetail=$('aside','ht-inspector');const side=$('aside','ht-graph-side');side.append(this.nodeDetail);
            const grid=$('div','ht-split');grid.append(this.canvas,side);
            this.labelNotice=$('p','ht-legend ht-label-note');this.labelNotice.setAttribute('aria-live','polite');
            const legend=$('div','ht-legend ht-category-legend');for(const [kind,label] of [['QUERY_OPERATION','Query'],['MUTATION_OPERATION','Mutation'],['INPUT_OBJECT','Inputs'],['ENUM','Enum'],['OBJECT','Other schema types']]){const item=$('span','',label);item.dataset.kind=kind;legend.append(item);}
            panel.append(bar,this.snapshotMeta,legend,$('p','ht-legend','Schema Overview condenses large graphs for performance. Select a node to open its focus view; search or class filters open detailed views.'),this.graphInfo,this.labelNotice,grid);
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
            this.graphInfo.textContent='Loading schema · nodes and cross-page relationships are being combined …';
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
                        if(page.schema_fingerprint!==scope.schema_fingerprint||!Array.isArray(page.nodes)||!Array.isArray(page.edges))throw new Error('The schema response does not match the requested context.');
                        coverage=coverage||page.coverage;
                        if(page.total!==coverage.nodes||page.coverage.nodes!==coverage.nodes||page.coverage.edges!==coverage.edges)throw new Error('The graph size changed while loading.');
                        for(const node of page.nodes){if(nodes.has(node.node_id))throw new Error('Duplicate node identity in the page contract.');nodes.set(node.node_id,node);}addEdges(page.edges);
                        this.graphInfo.textContent=`Loading · ${nodes.size}/${coverage.nodes} nodes · ${edges.size}/${coverage.edges} relationships`;
                        if(!page.has_more)break;if(!page.nodes.length)throw new Error('An empty follow-up page violated the graph contract.');start+=page.nodes.length;
                    }
                    if(nodes.size!==coverage.nodes)throw new Error('Not all nodes were delivered.');
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
                            if(data.schema_fingerprint!==scope.schema_fingerprint||data.node?.node_id!==id)throw new Error('The node detail does not belong to the requested schema.');
                            addEdges([...data.incoming,...data.outgoing]);
                            this.graphInfo.textContent=`Loading · ${nodes.size}/${coverage.nodes} nodes · ${edges.size}/${coverage.edges} relationships`;
                        }
                    }));
                    if(!current())return;
                    if(edges.size!==coverage.edges||[...edges.values()].some(e=>!nodes.has(e.source_id)||!nodes.has(e.target_id)))throw new Error('The complete relationship set could not be verified.');
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
                this.updateSnapshotSelector(full.coverage);
                const filtered=!!(filter.search||filter.kind||filter.relation),matches=new Set(full.nodes.filter(n=>(!filter.search||[n.node_id,n.name,n.type_ref||''].join(' ').toLocaleLowerCase().includes(filter.search))&&(!filter.kind||n.kind===filter.kind)).map(n=>n.node_id));
                const allowedEdges=full.edges.filter(e=>!filter.relation||e.kind===filter.relation),ids=new Set(matches);
                // Explicit semantic boundary: matching nodes plus their immediate partners.
                for(const edge of allowedEdges)if(matches.has(edge.source_id)||matches.has(edge.target_id)){ids.add(edge.source_id);ids.add(edge.target_id);}
                const graph={nodes:full.nodes.filter(n=>ids.has(n.node_id)),edges:allowedEdges.filter(e=>ids.has(e.source_id)&&ids.has(e.target_id)),coverage:full.coverage};
                this.graphFilterActive=filtered;
                const signature=JSON.stringify([key,filter]);if(signature!==this.graphSignature)this.viewbox=null;this.graphSignature=signature;this.graph=graph;
                this.graphInfo.textContent=`${filtered?'Filtered view: '+matches.size+' matches + direct relationship partners':'Full Schema'} · ${graph.nodes.length} nodes · ${graph.edges.length} relationships · fully loaded: ${full.nodes.length}/${full.coverage.nodes} nodes, ${full.edges.length}/${full.coverage.edges} relationships${full.coverage.introspection_complete?'':' · saved introspection is incomplete'}`;
                if(!ids.has(this.selectedNode))this.selectedNode=null;
                const id=new URLSearchParams(location.search).get('node')||this.selectedNode||graph.nodes[0]?.node_id;
                if(id&&ids.has(id))await this.inspectNode(id,serial,false);else this.drawGraph();
            } catch(error){if(current()){controller.abort();this.graph=null;this.canvas.replaceChildren();this.canvas._heroScene=null;this.graphInfo.textContent='The graph did not load completely: '+error.message+' Choose “Full Schema” or “Filtered View” to try again.';this.labelNotice.textContent='No complete visualization is available.';}}
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
            const labels={QUERY_ROOT:'Query Root',MUTATION_ROOT:'Mutation Root',SUBSCRIPTION_ROOT:'Subscription Root',QUERY_OPERATION:'Queries',MUTATION_OPERATION:'Mutations',SUBSCRIPTION_OPERATION:'Subscriptions',OBJECT:'Object Types',INPUT_OBJECT:'Input Objects',INTERFACE:'Interfaces',UNION:'Unions',ENUM:'Enums',ENUM_VALUE:'Enum Values',SCALAR:'Scalars',FIELD:'Fields',ARGUMENT:'Arguments',INPUT_FIELD:'Input Fields',DIRECTIVE:'Directives'};
            const slots={QUERY_ROOT:[0,0],MUTATION_ROOT:[0,1],SUBSCRIPTION_ROOT:[0,2],QUERY_OPERATION:[1,0],MUTATION_OPERATION:[1,1],SUBSCRIPTION_OPERATION:[1,2],OBJECT:[2,0],INTERFACE:[2,1],UNION:[2,2],INPUT_OBJECT:[2,3],ENUM:[2,4],SCALAR:[2,5],FIELD:[3,0],INPUT_FIELD:[3,3],ENUM_VALUE:[3,4],ARGUMENT:[4,0],DIRECTIVE:[4,5]};
            const counts=new Map(),nodeKinds=new Map(graph.nodes.map(node=>[node.node_id,node.kind]));for(const node of graph.nodes)counts.set(node.kind,(counts.get(node.kind)||0)+1);
            const cards=new Map(),cardWidth=250,cardHeight=82,xStep=300,yStep=104;
            for(const [kind,count] of counts){const slot=slots[kind]||[4,4],x=30+slot[0]*xStep,y=30+slot[1]*yStep;cards.set(kind,{kind,count,x,y,width:cardWidth,height:cardHeight});}
            const overviewWidth=30+5*xStep,overviewHeight=30+6*yStep,fitBox=[0,0,overviewWidth,overviewHeight];box=box||[...fitBox];canvas.setAttribute('viewBox',box.join(' '));canvas.replaceChildren();
            const defs=svg('defs'),marker=svg('marker',{id:markerId,viewBox:'0 0 10 10',refX:9,refY:5,markerWidth:6,markerHeight:6,orient:'auto-start-reverse'});marker.append(svg('path',{d:'M 0 0 L 10 5 L 0 10 z','class':'ht-arrow'}));defs.append(marker);canvas.append(defs);
            const aggregate=new Map();for(const edge of graph.edges){const from=nodeKinds.get(edge.source_id),to=nodeKinds.get(edge.target_id);if(!from||!to||from===to||!cards.has(from)||!cards.has(to))continue;const key=from+'→'+to;aggregate.set(key,{from,to,count:(aggregate.get(key)?.count||0)+1});}
            for(const item of aggregate.values()){const a=cards.get(item.from),b=cards.get(item.to),forward=b.x>=a.x,x1=forward?a.x+a.width:a.x,x2=forward?b.x:b.x+b.width,y1=a.y+a.height/2,y2=b.y+b.height/2,mid=(x1+x2)/2;const path=svg('path',{d:`M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2},${y2}`,'class':'ht-edge','marker-end':`url(#${markerId})`,'stroke-width':Math.min(5,1+Math.log10(item.count+1)),'stroke-opacity':'.5'}),title=svg('title');title.textContent=`${labels[item.from]||item.from} → ${labels[item.to]||item.to}: ${item.count} relationships`;path.append(title);canvas.append(path);}
            const selectedKind=selected?nodeKinds.get(selected):null;
            for(const card of cards.values()){const label=labels[card.kind]||card.kind,g=svg('g',{'data-overview-kind':card.kind,'data-kind':card.kind,'class':'ht-node'+(card.kind===selectedKind?' is-selected':''),tabindex:0,role:'button','aria-label':`${label}: ${card.count} nodes. Open filtered view.`});
                g.append(svg('rect',{x:card.x,y:card.y,width:card.width,height:card.height,rx:8}),svg('rect',{x:card.x+2,y:card.y+2,width:6,height:card.height-4,rx:3,'class':'ht-kind-mark'}));
                const title=svg('text',{x:card.x+14,y:card.y+28});title.textContent=label;const count=svg('text',{x:card.x+14,y:card.y+55});count.textContent=`${card.count} nodes`;g.append(title,count);
                const open=()=>{this.graphSearch.value='';this.graphKind.value=card.kind;this.relationKind.value='';this.loadGraph();};g.addEventListener('click',open);g.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();open();}});canvas.append(g);
            }
            canvas.setAttribute('data-total-nodes',graph.nodes.length);canvas.setAttribute('data-total-edges',graph.edges.length);canvas._heroScene={mode:'overview',box,fitBox,nodes:[...cards.values()].map(card=>({x:card.x,y:card.y,width:card.width,height:card.height})),selected,notice};notice.textContent=`${this.graphFilterActive?'Filtered overview':'Schema Overview'}: ${cards.size} class cards condense ${graph.nodes.length} saved nodes and ${graph.edges.length} relationships. This is not an incomplete graph; select a class card to open its filtered detail view.`;return box;
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
            scene.notice.textContent=(scene.compact?'Overview without node text; use selection and Inspector for names and details. ':'')+(routes.length>placed.length?`Reduced edge labels: ${placed.length} of ${routes.length} visible relationships are labeled. All relationships remain available in node focus and Inspector.`:'All visible relationships are labeled.');
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
        rememberButton(node,scope){
            // Only rendered for a real, single node detail; overview class cards never reach detailContents.
            const store=window.hero_integration_bookmarks;if(!store||!node?.node_id||!scope?.schema_fingerprint)return null;
            const identity={snapshot_name:scope.connection_id,schema_fingerprint:scope.schema_fingerprint,node_id:node.node_id,node_name:node.name,node_kind:node.kind};
            const b=button('Remember for Mapping',()=>{if(this.scopeKey(scope)!==this.scopeKey()){this.notice.textContent=errors.SCHEMA_CHANGED;return;}store.remember(identity);});
            b.classList.add('ht-remember');b.title='Remember this node under your own alias for the Mapping Editor. No mapping is created.';return b;
        }
        detailContents(target,data,scope,navigate){
            target.replaceChildren($('h3','',data.node.name),$('p','',data.node.node_id),$('p','',data.node.description||''),$('pre','ht-data',JSON.stringify(data.node,null,2)));
            const remember=this.rememberButton(data.node,scope);if(remember)target.insertBefore(remember,target.children[2]);
            if(this.tool==='analyzer')this.appendAnalyzerService(target,data.node,scope);
            for(const [title,edges,key] of [['Incoming',data.incoming,'source_id'],['Outgoing',data.outgoing,'target_id']]){target.append($('h4','',`${title} (${edges.length})`));for(const edge of edges)target.append(button(`${edge.kind}${edge.label?' · '+edge.label:''} · ${edge[key]}`,()=>navigate(edge[key])));}
        }
        appendAnalyzerService(target,node,scope){
            this.analyzerServiceForm=null;this.analyzerCreateButton=null;
            if(!['QUERY_OPERATION','MUTATION_OPERATION'].includes(node.kind))return;
            const button= this.analyzerCreateButton = document.createElement('button');
            button.className='btn btn-default';button.type='button';button.textContent='Save as GraphQL Service';
            button.title='Creates an unsaved service draft from this operation. No HERO operation is executed.';
            const box=$('section','ht-analyzer-service');target.append(button,box);
            button.addEventListener('click',()=>this.run(()=>this.openAnalyzerService(box,node,scope)));
            this.updateActions();
        }
        async openAnalyzerService(target,node,scope){
            const serial=this.sequence,generation=this.analyzerDetailGeneration;
            const draft=await call('get_analyzer_service_draft',{...scope,schema_node_id:node.node_id});
            if(serial!==this.sequence||generation!==this.analyzerDetailGeneration||this.scopeKey(scope)!==this.scopeKey())return;
            const name=input(),query=$('textarea','form-control');
            name.value=draft.payload.name;query.value=draft.payload.query;query.rows=12;
            name.setAttribute('aria-label','Service name');query.setAttribute('aria-label','GraphQL draft');
            const save=button('Save Service',()=>this.run(()=>this.saveAnalyzerService()));
            this.analyzerServiceForm={target,scope,nodeId:node.node_id,draft,name,query,save,saved:false};
            target.replaceChildren($('h3','',draft.operation_kind==='mutation'?'Save Mutation':'Save Query'),$('p','','Provenance=API Analyzer'),
                $('p','ht-context','Saves a service without executing it. For object results, __typename is preselected; add the required fields.'),
                $('p','ht-context',draft.required_variables.length?'Required variables: '+draft.required_variables.map(v=>v.name+' ('+v.type+')').join(', ')+'. Enter their values in GraphQL Services → Variables; values are never stored with the service.':'No required variables.'),
                field('Service name',name),field('GraphQL',query),save);
            this.updateActions();
        }
        async saveAnalyzerService(){
            const form=this.analyzerServiceForm;if(!form||form.saved)return;
            if(this.scopeKey(form.scope)!==this.scopeKey())throw new Error(errors.BINDING_CHANGED);
            // Variable values are runtime input only and are not part of the saved service definition.
            const {variables:_runtimeOnly,...draftPayload}=form.draft.payload;
            const payload={...draftPayload,name:form.name.value,query:form.query.value};
            const result=await call('save_analyzer_service',{...form.scope,schema_node_id:form.nodeId,payload},true);
            form.saved=true;form.save.disabled=true;
            if(this.analyzerServiceForm!==form)return;
            const link=$('a','btn btn-default','Open in GraphQL Services');link.href=this.serviceUrl(result.query.service_kind,result.query.query_id);
            link.addEventListener('click',event=>{if(this.openService(result.query.service_kind,result.query.query_id)===false)event.preventDefault();});
            form.target.replaceChildren($('p','',result.query.name+' saved · Provenance='+result.query.provenance),link);
            this.notice.textContent='Service saved. No HERO operation was executed.';
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
                this.focusScalar=$('section','ht-scalar-focus');this.focusScalar.hidden=true;this.focusScalar.setAttribute('aria-label','GraphQL base scalar usages');
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
            } catch(error){if(current())this.focusNotice.textContent='The focus view could not be loaded: '+error.message;}
        }
        focusNext(id){if(id!==this.focusHistory.at(-1))this.focusHistory.push(id);this.loadFocus(id);}
        drawFocus(){if(this.focusGraph)this.focusViewbox=this.renderGraphScene(this.focusCanvas,this.focusGraph,this.focusNode,this.focusViewbox,id=>this.focusNext(id),this.focusNotice,'hero-focus-arrow');}
        zoomFocus(factor){if(!this.focusViewbox)return;const next=this.boundedZoom(this.focusCanvas,this.focusViewbox,factor);if(next===this.focusViewbox)return;this.focusViewbox=next;this.applyFocusViewbox();}
        serviceLabel() { return this.serviceKind === 'mutation' ? 'Mutation' : 'Query'; }
        serviceScope(schema=false) { return {...this.scope(schema), operation_kind:this.serviceKind}; }
        serviceUrl(kind, queryId='') {
            const platform=window.hero_integration_platform;
            const target=platform
                ? new URL(platform.url('api',{hero_tool:'services',operation_kind:kind,query:queryId||null}), location.origin)
                : new URL(location.href);
            if(!platform){target.searchParams.set('hero_section','api');target.searchParams.set('hero_tool','services');target.searchParams.set('operation_kind',kind);target.searchParams.delete('query');if(queryId)target.searchParams.set('query',queryId);}
            return target.pathname+target.search+target.hash;
        }
        openService(kind, queryId='') {
            if(this.busy || (this.tool==='services' && !this.confirmReplace())) return false;
            const ui=window.hero_integration_ui;
            if(!ui) return true;
            ui.setRoute('api',{hero_tool:'services',operation_kind:kind,query:queryId||null});
            return false;
        }
        buildServices() {
            const p=this.panels.services,grid=$('div','ht-split');this.queryList=$('div','ht-list');const editor=$('section','ht-query-editor');this.name=input();this.category=input();this.category.value=this.serviceKind==='mutation'?'Custom Mutations':'Custom Queries';this.operation=input();this.description=$('textarea','form-control');
            const views=$('nav','ht-toolbar');views.setAttribute('aria-label','GraphQL Services');
            for(const [kind,label] of [['query','Query'],['mutation','Mutation']]) {
                const link=$('a','btn btn-default',label);link.href=this.serviceUrl(kind);
                if(kind===this.serviceKind)link.setAttribute('aria-current','page');
                link.addEventListener('click',e=>{if(this.openService(kind)===false)e.preventDefault();});views.append(link);
            }
            this.editorHost=$('div','ht-editor');this.variables=$('textarea','form-control ht-variables');this.variables.value='';this.variables.rows=6;this.variables.spellcheck=false;this.variables.placeholder='id := 5\nsearch := "Photovoltaik"\nactive := true';this.queryOutput=$('pre','ht-data');this.queryOutput.setAttribute('aria-live','polite');this.queryStatus=$('p','ht-query-status');this.queryStatus.setAttribute('role','status');
            const bar=$('div','ht-toolbar');this.newButton=button('New '+this.serviceLabel(),()=>this.newQuery());this.copyButton=button('Duplicate',()=>this.run(async()=>this.newQuery(true)));this.saveButton=button('Save',()=>this.run(()=>this.saveQuery()));this.validateButton=button('Validate',()=>this.run(()=>this.validateQuery()));this.executeButton=button(this.serviceKind==='mutation'?'Execute Mutation':'Execute Query',()=>this.run(()=>this.executeQuery()));this.deleteButton=button('Delete',()=>this.run(()=>this.deleteQuery()));bar.append(this.newButton,this.copyButton,this.saveButton,this.validateButton,this.executeButton,this.deleteButton);
            this.serviceHint=$('p','ht-service-hint');this.queryProvenance=$('p','ht-query-provenance');
            // Variables are runtime input: one "name := value" line each, converted to a JSON object before a request.
            this.variablesHelp=$('p','ht-context','One variable per line: name := value. Strings use double quotes; numbers, true, false and null have no quotes; arrays and objects use JSON. Values are not stored with the service.');
            this.variablesJson=$('pre','ht-data ht-variables-json');const advanced=$('details','ht-variables-advanced');advanced.append($('summary','','Advanced: JSON variables'),this.variablesJson);
            editor.append(this.queryStatus,this.queryProvenance,field('Name',this.name),field('Category',this.category),field('Description',this.description),field('operationName',this.operation),$('h3','','GraphQL'),this.editorHost,field('Variables',this.variables),this.variablesHelp,advanced,bar,this.queryOutput);grid.append(this.queryList,editor);this.queryPager=this.pager('services',()=>this.loadQueries());p.append(views,$('h2','','GraphQL Services · '+this.serviceLabel()),this.serviceHint,grid,this.queryPager.root);
            for(const n of [this.name,this.category,this.description,this.operation])n.addEventListener('input',()=>this.queryChanged());
            this.variables.addEventListener('input',()=>{this.renderVariablesJson();this.updateActions();});this.renderVariablesJson();
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
        queryString(q) {
            // Match SavedQuery's text normalization. Variable values are runtime input and never part of the saved definition,
            // so they do not mark the service as changed.
            return JSON.stringify([(q.name||'').trim(),(q.category||'').trim()||(this.serviceKind==='mutation'?'Custom Mutations':'Custom Queries'),(q.description||'').trim(),(q.operation_name||'').trim(),(q.query||'').trim()]);
        }
        draftString(){return this.queryString({name:this.name.value,category:this.category.value,description:this.description.value,operation_name:this.operation.value,query:this.editor?.getValue()||''});}
        variablesValue(){return parseVariablesText(this.variables.value);}
        renderVariablesJson(){if(!this.variablesJson)return;try{this.variablesJson.textContent=JSON.stringify(this.variablesValue(),null,2);this.variables.removeAttribute('aria-invalid');}catch(error){this.variablesJson.textContent=error.message;this.variables.setAttribute('aria-invalid','true');}}
        get dirty(){return this.base!==undefined&&this.base!==this.draftString();}
        confirmReplace(){return !this.dirty||window.confirm('Discard the unsaved service draft?');}
        payload(){return {name:this.name.value,category:this.category.value.trim()||(this.serviceKind==='mutation'?'Custom Mutations':'Custom Queries'),description:this.description.value,operation_name:this.operation.value||null,query:this.editor.getValue()};}
        setQuery(q={}){this.copiedFrom=q.copied_from||'';this.draftProvenance=q.provenance||'GraphQL-Services';this.queryEpoch=(this.queryEpoch||0)+1;this.query=q.query_id?q:null;this.name.value=q.name||'';this.category.value=q.category||(this.serviceKind==='mutation'?'Custom Mutations':'Custom Queries');this.description.value=q.description||'';this.operation.value=q.operation_name||'';this.editor.setValue(q.query||'',-1);if(!q.keep_variables)this.variables.value=formatVariablesText(q.variables);this.renderVariablesJson();this.base=this.draftString();this.validation=null;this.queryOutput.textContent=q.query_id?`${this.serviceLabel()} ${q.query_id} · Revision ${q.revision}`:'New unsaved draft';this.updateActions();}
        newQuery(copy=false){if(!copy&&!this.confirmReplace())return;const q=copy?{...this.payload(),name:this.name.value+' (Copy)',copied_from:this.query?.query_id||this.copiedFrom,provenance:this.query?.provenance||this.draftProvenance,keep_variables:true}:{};this.setQuery(q);if(copy)this.base='';}
        async loadQueries(serial=this.sequence){const data=await call('list_services',{...this.serviceScope(),start:this.starts.services,page_size:50});if(serial!==this.sequence)return;this.mutationExecution=data.mutation_execution||null;this.queryList.replaceChildren($('h3','',this.serviceKind==='mutation'?'Saved Mutations':'Saved Queries'));for(const q of data.rows)this.queryList.append(button(`${q.name} · r${q.revision} · Provenance=${q.provenance||'Legacy (unbekannt)'}`,()=>{if(!this.busy&&this.confirmReplace())this.setQuery(q);}));this.queryPager.update(data.has_more);
            const queryId=new URLSearchParams(location.search).get('query');
            if(queryId && queryId!==this.loadedQueryId && queryId!==this.query?.query_id && !this.dirty) {const result=await call('get_query',{...this.scope(),query_id:queryId});if(serial===this.sequence){if(result.query.service_kind!==this.serviceKind){this.openService(result.query.service_kind,queryId);return;}this.setQuery(result.query);this.loadedQueryId=queryId;}}
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
        async validateQuery(){
            // Non-empty variables are checked locally against the document's declarations; nothing is sent to HERO.
            const variables=this.variables.value.trim()?{variables:this.variablesValue()}:{};
            const stamp=this.draftString(),queryId=this.query?.query_id||'';
            const data=this.query&&!this.dirty?await call('validate_saved_query',{...this.serviceScope(true),query_id:this.query.query_id,expected_revision:this.query.revision,...variables},true):await call('preview_query',{...this.serviceScope(true),payload:this.payload(),...variables},true);
            if(stamp!==this.draftString()||queryId!==(this.query?.query_id||''))return;
            if(data.query&&this.query)this.query={...this.query,status:data.query.status};this.validation=data;this.queryOutput.textContent=JSON.stringify(data,null,2);this.updateActions();
        }
        canExecuteQuery(){return this.serviceKind==='query'&&!!this.schema;}
        async executeQuery(){
            // Mutations stay blocked here regardless of the global gate: HERO_MUTATION_EXECUTION_DISABLED.
            if(this.serviceKind!=='query')throw new Error(errors.HERO_MUTATIONS_DISABLED+' (HERO_MUTATION_EXECUTION_DISABLED)');
            const variables=this.variablesValue();
            // Never run an older saved revision silently: unsaved editor content is saved and validated first, on request only.
            if(!this.query||this.dirty){
                if(!window.confirm('The editor has unsaved changes. Save them as a new revision, validate the query and then execute it once?'))return;
                const before=this.queryEpoch;await this.saveQuery();
                if(before!==this.queryEpoch||!this.query||this.dirty)throw new Error('The editor changed while saving. Nothing was executed.');
            }
            const epoch=this.queryEpoch,stamp=this.draftString(),queryId=this.query.query_id,revision=this.query.revision;
            if(this.query.status!=='Validated'){
                const checked=await call('validate_saved_query',{...this.serviceScope(true),query_id:queryId,expected_revision:revision},true);
                if(epoch!==this.queryEpoch||queryId!==this.query?.query_id||revision!==this.query?.revision)return;
                if(checked?.query?.status!=='Validated')throw new Error('The saved query could not be validated. Nothing was executed.');
                this.query={...this.query,status:checked.query.status};this.validation=checked;
            }
            if(stamp!==this.draftString())throw new Error('The editor changed during validation. Nothing was executed.');
            this.queryOutput.textContent=`Executing ${this.serviceLabel()} ${queryId} · Revision ${revision} once against HERO …`;
            const result=await call('execute_service',{...this.serviceScope(true),query_id:queryId,expected_revision:revision,variables},true);
            if(epoch!==this.queryEpoch||queryId!==this.query?.query_id)return;
            this.renderExecution(result);
        }
        renderExecution(result){
            // Text only: HERO data is never interpreted as HTML. The full result opens in its own dialog.
            const head=`${result.operation||''} · Revision ${result.revision} · Schema ${String(result.schema_fingerprint||'').slice(0,12)}…`;
            const label={OK:'HERO response',PARTIAL:'Partial HERO response with GraphQL errors',TOO_LARGE:'HERO response too large to display'}[result.status]
                ||{GRAPHQL:'GraphQL error returned by HERO',TRANSPORT:'HTTP/transport error',RESPONSE:'Invalid HERO response'}[result.error_kind]||'HERO request failed';
            this.queryOutput.textContent=`${label} (${result.status}${result.error_code?' · '+result.error_code:''}) · ${head}${result.size_bytes!==undefined?' · '+result.size_bytes+' bytes':''}\nThe result is shown in the GraphQL Query Result window and is not stored.`;
            this.showQueryResult(result,label,head);
        }
        showQueryResult(result,label,head){
            this.resultDialog?.hide?.();
            const hasData=(result.status==='OK'||result.status==='PARTIAL')&&result.data!==undefined;
            const views={readable:hasData?readableResult(result.data):'',raw:hasData?JSON.stringify(result.data,null,2):''};
            const body=$('div','ht-result');
            const status=$('div','ht-result-status');status.dataset.state=result.status;status.setAttribute('role','status');
            status.append($('strong','',label),$('div','',head));
            if(result.size_bytes!==undefined)status.append($('div','',`Response size: ${result.size_bytes} bytes${result.limit_bytes?' (display limit '+result.limit_bytes+' bytes)':''}`));
            const summary=hasData?resultSummary(result.data):[];
            if(summary.length)status.append($('div','','Delivered: '+summary.join(' · ')));
            body.append(status);
            // HTTP/transport failures and GraphQL errors are labelled separately; partial data stays visible below.
            if(result.status==='ERROR'||result.status==='PARTIAL'||result.status==='TOO_LARGE'){
                const errorsBox=$('div','ht-result-errors');errorsBox.setAttribute('role','alert');
                errorsBox.append($('strong','',result.error_kind==='GRAPHQL'?'GraphQL errors':result.error_kind==='TRANSPORT'?'HTTP/transport error':result.error_kind==='RESPONSE'?'Invalid response':'Not displayed'),$('div','',result.message||''));
                const list=$('ul','');for(const message of result.graphql_errors||[])list.append($('li','',message));if(list.childElementCount)errorsBox.append(list);
                body.append(errorsBox);
            }
            const toolbar=$('div','ht-toolbar ht-result-views');const output=$('pre','ht-result-output');output.setAttribute('tabindex','0');
            let view='readable';
            const show=next=>{view=next;output.textContent=hasData?views[next]:(result.status==='TOO_LARGE'?'The response was not displayed because it exceeds the display limit. Nothing was truncated silently.':'No data was delivered.');for(const [key,b] of buttons)b.setAttribute('aria-pressed',String(key===next));};
            const buttons=[['readable',button('Readable',()=>show('readable'))],['raw',button('Raw JSON',()=>show('raw'))]];
            for(const [,b] of buttons){b.disabled=!hasData;toolbar.append(b);}
            const copy=button('Copy Result',()=>{const text=output.textContent;if(window.frappe?.utils?.copy_to_clipboard)frappe.utils.copy_to_clipboard(text);else navigator.clipboard?.writeText(text);});
            toolbar.append(copy);output.setAttribute('aria-label','GraphQL Query Result');body.append(toolbar,output);show('readable');
            const dialog=new frappe.ui.Dialog({title:'GraphQL Query Result',size:'extra-large',fields:[{fieldtype:'HTML',fieldname:'result'}],primary_action_label:'Close',primary_action:()=>dialog.hide()});
            dialog.$wrapper.addClass('admin-compat-modal ht-result-dialog');
            dialog.fields_dict.result.$wrapper[0].replaceChildren(body);
            this.resultDialog=dialog;dialog.show();
        }
        async deleteQuery(){if(!this.query||!window.confirm(this.dirty?'Delete the saved service and discard the unsaved draft?':'Delete the saved service?'))return;await call('delete_query',{...this.serviceScope(),query_id:this.query.query_id,expected_revision:this.query.revision},true);this.setQuery();await this.loadQueries();}
        updateActions(){if(!this.saveButton)return;
            const analyzerAllowed=!!this.context?.can_write&&!this.context.needs_binding_update&&!this.busy;
            if(this.analyzerCreateButton)this.analyzerCreateButton.disabled=!analyzerAllowed||!!this.analyzerServiceForm;
            if(this.analyzerServiceForm)this.analyzerServiceForm.save.disabled=!analyzerAllowed||this.analyzerServiceForm.saved;
            if(this.queryProvenance)this.queryProvenance.textContent='Provenance='+(this.query?.provenance||this.draftProvenance||'GraphQL-Services');
            const gate=this.mutationExecution;
            if(this.serviceHint)this.serviceHint.textContent=this.serviceKind==='mutation'
                ? `Mutation drafts are local-only, stored locally, and validated only against the saved schema. Execution from this editor is blocked. Server mutation gate: ${gate?.mutation_execution_status||'unknown'}${gate?.mutation_execution_reason?' · '+gate.mutation_execution_reason:''}`
                : 'Execute Query reads data from HERO once on an explicit click, using the saved and validated revision. Results open in a separate window and are never stored, synchronized or written to Frappe.';
this.queryStatus.textContent=this.query?`${this.serviceLabel()} ${this.query.query_id} · Revision ${this.query.revision}${this.dirty?' · Unsaved changes':''}`:'New unsaved draft';const allowed=!!this.context?.can_write&&!this.context.needs_binding_update&&!this.busy&&!!this.schema;for(const b of [this.newButton,this.copyButton])b.disabled=!allowed;this.saveButton.disabled=!allowed;this.saveButton.title=this.serviceKind==='mutation'?'Stores a local-only disabled mutation draft.':' ';this.validateButton.disabled=!allowed;this.executeButton.disabled=!allowed||!this.canExecuteQuery();this.executeButton.title=this.serviceKind==='mutation'?'HERO_MUTATION_EXECUTION_DISABLED':this.dirty||!this.query?'Asks to save and validate the changes, then runs that revision once, read-only.':'Runs the saved revision once, read-only.';this.deleteButton.disabled=!allowed||!this.query;this.bindButton.disabled=!this.context?.can_write||this.busy;}
        destroy(){
            this.cancelGraph?.();
            this.focusDialog?.hide?.();
            this.resultDialog?.hide?.();
            this.resize?.disconnect?.();
            this.editor?.destroy?.();
            this.editor=null;
            if(this.beforeUnloadHandler){
                window.removeEventListener('beforeunload',this.beforeUnloadHandler);
            }
            this.root?.remove?.();
        }
    }
    window.hero_workspace_tools={Workspace,graphqlText:Object.freeze({parseVariablesText,formatVariablesText,readableResult,resultSummary})};
})();
