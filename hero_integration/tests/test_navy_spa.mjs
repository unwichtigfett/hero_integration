import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const extension = readFileSync(new URL('../public/js/navy_spa.js', import.meta.url), 'utf8');

function node(label = '') {
    const attributes = new Map(), handlers = new Map();
    const self = {children: [], label: {textContent: label},
        hasAttribute: name => attributes.has(name), setAttribute: (name, value) => attributes.set(name, value),
        matches: () => true, addEventListener: (name, handler) => handlers.set(name, handler),
        click: () => handlers.get('click')?.(), key: key => handlers.get('keydown')({key, preventDefault() {}}),
        get lastElementChild() { return {firstElementChild: self.label}; },
        cloneNode: () => node(self.label.textContent),
        appendChild(child) { self.children.push(child); },
        querySelector: () => self.children.find(child => child.hasAttribute('data-hero-navy-card')) || null};
    return self;
}

function setup({saved = 'light', theme = saved, stored = null, systemDark = false, settingsOpen = true} = {}) {
    const attributes = new Map(theme ? [['data-theme', theme]] : []);
    const storage = new Map([...(stored ? [['hero-theme', stored]] : []), ...(saved ? [['theme', saved]] : [])]);
    const timers = [];
    const observers = [];
    const root = {getAttribute: name => attributes.has(name) ? attributes.get(name) : null,
        hasAttribute: name => attributes.has(name), removeAttribute: name => attributes.delete(name),
        setAttribute(name, value) { attributes.set(name, value); if (name === 'data-theme') observers[0](); }};
    class MutationObserver { constructor(callback) { observers.push(callback); } observe() {} }
    const row = node();
    for (const label of ['Light', 'Dark', 'System']) row.children.push(node(label));
    const page = {open: settingsOpen};
    const context = vm.createContext({MutationObserver,
        document: {documentElement: root, body: {}, querySelectorAll: () => page.open ? [row] : []},
        window: {setTimeout: callback => timers.push(callback), matchMedia: () => ({matches: systemDark}),
            localStorage: {getItem: key => storage.has(key) ? storage.get(key) : null,
                setItem: (key, value) => storage.set(key, value)}}});
    vm.runInContext(extension, context);
    const settle = () => { while (timers.length) timers.shift()(); };
    // What Helpdesk and CRM do when the user picks one of their own themes.
    const choose = (index, value) => { storage.set('theme', value); root.setAttribute('data-theme', value); row.children[index].click(); settle(); };
    const state = () => [attributes.get('data-theme'), attributes.get('data-hero-theme') || null];
    const navyCard = () => row.querySelector();
    const rerender = () => { observers[1](); settle(); };
    return {context, root, storage, row, page, state, settle, choose, navyCard, rerender};
}

test('Navy is offered as one more card beside light, dark and system', () => {
    const {row, navyCard, rerender, context} = setup();
    assert.deepEqual(row.children.map(card => card.label.textContent), ['Light', 'Dark', 'System', 'Navy']);
    assert.ok(navyCard().hasAttribute('data-hero-navy-card'));
    rerender();
    vm.runInContext(extension, context);
    assert.equal(row.children.length, 4);
});

test('the card appears when the settings are opened later', () => {
    const {row, page, rerender} = setup({settingsOpen: false});
    assert.equal(row.children.length, 3);
    page.open = true;
    rerender();
    assert.equal(row.children.length, 4);
});

test('light and dark are untouched until Navy is chosen', () => {
    for (const saved of ['light', 'dark']) {
        const {state, choose, storage} = setup({saved});
        assert.deepEqual(state(), [saved, null]);
        choose(1, 'dark'); assert.deepEqual(state(), ['dark', null]);
        choose(0, 'light'); assert.deepEqual(state(), ['light', null]);
        assert.equal(storage.has('hero-theme'), false);
    }
});

test('choosing Navy keeps the page\'s saved theme and paints Navy over dark', () => {
    for (const saved of ['light', 'dark', 'system']) {
        const {navyCard, state, storage} = setup({saved, theme: saved === 'dark' ? 'dark' : 'light'});
        navyCard().click();
        assert.deepEqual(state(), ['dark', 'navy']);
        assert.equal(storage.get('hero-theme'), 'navy');
        assert.equal(storage.get('theme'), saved);
    }
    const keyboard = setup();
    keyboard.navyCard().key('Enter');
    assert.deepEqual(keyboard.state(), ['dark', 'navy']);
});

test('choosing one of the page\'s own themes ends Navy, also the one already saved', () => {
    for (const [saved, index, chosen] of [['light', 1, 'dark'], ['dark', 0, 'light'], ['dark', 1, 'dark'], ['light', 0, 'light']]) {
        const {navyCard, state, choose, storage} = setup({saved});
        navyCard().click();
        choose(index, chosen);
        assert.deepEqual(state(), [chosen, null]);
        assert.equal(storage.get('hero-theme'), 'standard');
    }
    const system = setup({saved: 'system', theme: 'dark', systemDark: true});
    system.navyCard().click();
    system.choose(2, 'system');
    assert.deepEqual(system.state(), ['dark', null]);
});

test('a saved Navy choice survives the page restoring its saved theme on load', () => {
    for (const saved of ['light', 'dark']) {
        const {root, state, settle} = setup({saved, theme: null, stored: 'navy'});
        assert.deepEqual(state(), ['dark', 'navy']);
        root.setAttribute('data-theme', saved); settle();
        assert.deepEqual(state(), ['dark', 'navy']);
    }
});
