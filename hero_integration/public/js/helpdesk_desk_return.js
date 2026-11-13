(() => {
    "use strict";

    // Loaded in Helpdesk only while its setting asks for the Desk's start page: the "Desk"
    // entry of the Apps menu then skips the Helpdesk workspace inside the Desk.
    if (window.hero_desk_return) return;
    window.hero_desk_return = true;

    const open = window.open;
    window.open = function (url, ...rest) {
        return open.call(this, url === "/desk/helpdesk" ? "/desk" : url, ...rest);
    };
})();
