/* Page core: when the live-update stream (/api/stream) should be open.
   An open stream counts as a viewer on the server, which keeps every source
   polling at full speed (idle mode slows polling 8x only while no stream is
   open). A page nobody is looking at must therefore let go of its stream:
   - hidden (background tab, minimised window): closed at once;
   - visible but unfocused with no pointer or key input for IDLE_CLOSE_MS
     (a dashboard pane left open beside other work): closed;
   - visible and focused (a dashboard someone is watching): stays open.
   Any focus, input or return to visible reopens it; the first tick then
   refreshes whatever changed while it was closed.
   Pure state: the caller supplies open(), close() and now(), and feeds the
   page events in. Exports window.AdStreamGate = {IDLE_CLOSE_MS, create}. */
(function (global) {
  "use strict";
  var IDLE_CLOSE_MS = 10 * 60 * 1000;

  function create(opts) {
    var now = opts.now;
    var idleMs = opts.idleMs == null ? IDLE_CLOSE_MS : opts.idleMs;
    var open = false;
    var hidden = false;
    var focused = true;
    var lastInput = now();

    function wanted() {
      return !hidden && (focused || now() - lastInput < idleMs);
    }
    function sync() {
      var want = wanted();
      if (want && !open) { open = true; opts.open(); }
      else if (!want && open) { open = false; opts.close(); }
    }
    return {
      start: function (isHidden, hasFocus) {
        hidden = !!isHidden; focused = !!hasFocus; lastInput = now(); sync();
      },
      visibility: function (isHidden) {
        hidden = !!isHidden;
        if (!hidden) lastInput = now();
        sync();
      },
      focus: function (hasFocus) { focused = !!hasFocus; lastInput = now(); sync(); },
      input: function () { lastInput = now(); sync(); },
      check: sync,
      isOpen: function () { return open; }
    };
  }

  global.AdStreamGate = { IDLE_CLOSE_MS: IDLE_CLOSE_MS, create: create };
})(typeof window !== "undefined" ? window : globalThis);
