import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const extension = readFileSync(new URL('../public/js/site_logo.js', import.meta.url), 'utf8');

function element(className = '') {
    const classes = new Set(className ? [className] : []);
    const node = {children: [], parentNode: null, innerHTML: '', textContent: '',
        get className() { return [...classes].join(' '); },
        set className(value) { classes.clear(); if (value) classes.add(value); },
        classList: {contains: name => classes.has(name),
            toggle: (name, on) => on ? classes.add(name) : classes.delete(name)},
        get firstChild() { return this.children[0] || null; },
        remove() { this.parentNode.children.splice(this.parentNode.children.indexOf(this), 1); this.parentNode = null; },
        appendChild(child) { if (child.parentNode) child.remove(); this.children.push(child); child.parentNode = this; },
        insertBefore(child, before) {
            if (child.parentNode) child.remove();
            this.children.splice(this.children.indexOf(before), 0, child); child.parentNode = this;
        },
        querySelector(selector) {
            const name = selector.split('.').pop();
            for (const child of this.children) {
                if (child.classList.contains(name)) return child;
                const found = child.querySelector(selector);
                if (found) return found;
            }
            return null;
        }};
    return node;
}

function setup(logo, {docked = true, owns = true, app = {app_title: 'ERPNext'}} = {}) {
    const slot = {value: null, html(value) { this.value = value; }};
    const panel = element('body-sidebar');
    const rail = element('dock-utilities');
    const dockNode = element('dock');
    dockNode.appendChild(rail);
    for (const name of ['search', 'bell']) rail.appendChild(element(name));
    if (owns) dockNode.classList.toggle('dock-owns-utilities', true);
    const state = {app, docked};
    class Dock {
        constructor(sidebar) { this.$header_logo = slot; this.$dock = [dockNode]; this.sidebar = sidebar; }
        get enabled() { return state.docked; }
        apply_pin() { return 'pinned'; }
        render_logo() { this.$header_logo.html('<app>'); return 'done'; }
    }
    class SidebarHeader {
        constructor() { this.sidebar = {dock_enabled: () => state.docked, get_sidebar_app: () => state.app};
            this.sidebar.dock = new Dock(this.sidebar); }
        make() {
            const old = panel.querySelector('.sidebar-header'); if (old) old.remove();
            const node = element('sidebar-header'); panel.children.unshift(node); node.parentNode = panel;
            this.wrapper = [node]; return 'made';
        }
        refresh() { return 'refreshed'; }
        get_header_logo() { return '<module>'; }
    }
    const frappe = {boot: logo ? {hero_site_logo: logo} : {}, ui: {Dock, SidebarHeader},
        utils: {escape_html: value => value.replace(/"/g, '&quot;').replace(/</g, '&lt;'),
            app_logo: value => value ? {title: value.app_title} : null}};
    const create = () => {
        const row = element();
        let html = '';
        Object.defineProperty(row, 'innerHTML', {get: () => html, set(value) {
            html = value;
            for (const name of value.match(/hero-brand-[a-z]+/g) || []) row.appendChild(element(name));
        }});
        return row;
    };
    const context = vm.createContext({frappe,
        document: {createElement: create, querySelector: selector => panel.querySelector(selector)}});
    vm.runInContext(extension, context);
    vm.runInContext(extension, context);
    const header = new frappe.ui.SidebarHeader();
    const rows = () => panel.children.map(child => child.className);
    const part = name => panel.querySelector('.hero-brand-' + name);
    const names = node => node.children.map(child => child.className);
    return {slot, state, panel, rail, dockNode, rows, part, names, dock: header.sidebar.dock, header};
}

test('the rail top slot shows the site logo and apps keep their own header mark', () => {
    const {slot, dock, header} = setup('/files/logo.svg');
    assert.equal(dock.render_logo(), 'done');
    assert.equal(slot.value, '<img src="/files/logo.svg" alt="" />');
    assert.equal(header.get_header_logo(), '<module>');
});

test('the app name sits in one row above the module menu and survives rebuilds', () => {
    const {header, rows, part} = setup('/files/logo.svg');
    assert.equal(header.make(), 'made');
    assert.deepEqual(rows(), ['hero-brand-row', 'sidebar-header']);
    assert.equal(part('name').textContent, 'ERPNext');
    assert.equal(part('logo').innerHTML, '');
    assert.equal(header.refresh(), 'refreshed');
    header.make();
    assert.deepEqual(rows(), ['hero-brand-row', 'sidebar-header']);
});

test('search and notifications move beside the name while the rail owns them, and back', () => {
    const {header, dock, dockNode, rail, part, names, state} = setup('/files/logo.svg');
    header.make();
    assert.deepEqual(names(part('tools')), ['search', 'bell']);
    assert.deepEqual(names(rail), []);
    assert.ok(dockNode.classList.contains('hero-tools-beside-name'));
    header.refresh(); header.make();
    assert.deepEqual(names(part('tools')), ['search', 'bell']);
    dockNode.classList.toggle('dock-owns-utilities', false);
    assert.equal(dock.apply_pin(), 'pinned');
    assert.deepEqual(names(rail), ['search', 'bell']);
    assert.ok(!dockNode.classList.contains('hero-tools-beside-name'));
    dockNode.classList.toggle('dock-owns-utilities', true);
    dock.apply_pin();
    assert.deepEqual(names(part('tools')), ['search', 'bell']);
    state.app = null;
    header.refresh();
    assert.deepEqual(names(rail), ['search', 'bell']);
});

test('without a rail the row carries the site logo and the buttons stay where they are', () => {
    const {header, part, names, rail} = setup('/files/logo.svg', {docked: false});
    header.make();
    assert.equal(part('logo').innerHTML, '<img src="/files/logo.svg" alt="" />');
    assert.deepEqual(names(rail), ['search', 'bell']);
});

test('a sidebar outside any app gets no row, and no configured logo leaves the Desk unchanged', () => {
    const outside = setup('/files/logo.svg', {app: null});
    outside.header.make();
    assert.deepEqual(outside.rows(), ['sidebar-header']);
    const {slot, dock, header, rows, rail, names} = setup(null);
    dock.render_logo(); header.make(); dock.apply_pin();
    assert.equal(slot.value, '<app>');
    assert.deepEqual(rows(), ['sidebar-header']);
    assert.deepEqual(names(rail), ['search', 'bell']);
});
