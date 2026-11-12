# agentq shim library, sourced by the shell wrappers. POSIX sh; builtins only, so no fork on the fast path.

shim_real() {
    set -f
    _oldifs=$IFS
    IFS=:
    REAL=
    for _d in $PATH; do
        case $_d in
            /*) ;;
            *) continue ;;
        esac
        if [ "$_d" != "$SHIM_HERE" ] && [ -f "$_d/$1" ] && [ -x "$_d/$1" ]; then
            REAL=$_d/$1
            break
        fi
    done
    IFS=$_oldifs
    set +f
    if [ -z "$REAL" ]; then
        echo "agentq shim: no real \`$1\` on PATH outside $SHIM_HERE; put it on PATH or set AGENTQ_SHIMS=0" >&2
        return 1
    fi
    case $REAL in
        "$SHIM_HERE"/*)
            echo "agentq shim: real \`$1\` resolves into the shim folder ($REAL); refusing to recurse" >&2
            return 1
            ;;
    esac
    if [ "$REAL" -ef "$SHIM_HERE/$1" ]; then
        echo "agentq shim: real \`$1\` resolves to the shim itself ($REAL); refusing to recurse" >&2
        return 1
    fi
}

shim_dispatch() {
    exec "${AGENTQ_SHIM_PYTHON:-/usr/bin/python3}" -I "$SHIM_HERE/dispatch.py" "$@"
}
