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

shim_guarded_word() {
    case $1 in
        pytest|pyright|pre-commit|*ci-local.sh|*validate.sh|*select_pytests.py) return 0 ;;
    esac
    return 1
}

shim_dispatch() {
    exec "${AGENTQ_SHIM_PYTHON:-/usr/bin/python3}" -I "$SHIM_HERE/dispatch.py" "$@"
}

shim_refuse() {
    # shim_refuse <reason> <hint> <name> <args...>: log one guard_blocks.tsv row and exit 2.
    # The stderr line is "BLOCKED: agentq guard: <reason>", plus "; <hint>" when a hint is given.
    _reason=$1
    _hint=$2
    shift 2
    _log=${AGENTQ_HOME:-${HOME:-/}/.local/state/agentq}/guard_blocks.tsv
    _cmd=$(printf '%s' "$*" | tr '\t\n' '  ' | cut -c1-300)
    printf '%s\t%s\t%s\t%s\n' "$(date +%s)" "${AGENTQ_RUN:-${AGENTQ_JOB:--}}" "$_reason" "$_cmd" >> "$_log" 2>/dev/null
    if [ -n "$_hint" ]; then
        echo "BLOCKED: agentq guard: $_reason; $_hint" >&2
    else
        echo "BLOCKED: agentq guard: $_reason" >&2
    fi
    exit 2
}

shim_resolve() {
    # Sets RESOLVED to the physical path of $1, or to nothing when $1 is not a directory.
    RESOLVED=$(cd -- "$1" 2>/dev/null && pwd -P) || RESOLVED=
}

shim_broad_root() {
    # True when $1 resolves to a broad search root; sets BROAD_ROOT to the resolved path.
    # Broad: the system roots, the home folder, its projects and worktrees collections, any direct
    # child of the worktrees collection, and each line of $AGENTQ_HOME/guard_broad_roots.
    shim_resolve "$1"
    BROAD_ROOT=$RESOLVED
    [ -n "$BROAD_ROOT" ] || return 1
    shim_resolve "${HOME:-/nonexistent}"
    _home_r=$RESOLVED
    _worktrees=$_home_r/Documents/worktrees
    case $BROAD_ROOT in
        /|/Users|/System|/Library|/private|/Volumes|"$_home_r"|"$_home_r/Documents"|"$_home_r/Documents/projects"|"$_worktrees")
            return 0 ;;
    esac
    [ "${BROAD_ROOT%/*}" = "$_worktrees" ] && return 0
    _extra=${AGENTQ_HOME:-${HOME:-/}/.local/state/agentq}/guard_broad_roots
    if [ -f "$_extra" ]; then
        while IFS= read -r _line || [ -n "$_line" ]; do
            [ -n "$_line" ] || continue
            shim_resolve "$_line"
            [ "${RESOLVED:-$_line}" = "$BROAD_ROOT" ] && return 0
        done < "$_extra"
    fi
    return 1
}

shim_check_root() {
    # shim_check_root <name> <root> <argv...>: refuse the search when <root> is broad.
    _name=$1
    _root=$2
    shift 2
    if shim_broad_root "$_root"; then
        shim_refuse "$_name over $BROAD_ROOT refused" "search inside your own worktree or a specific folder" "$_name" "$@"
    fi
}

shim_sleep_budget() {
    # shim_sleep_budget <seconds> <sleep args...>: refuse when this job's running sleep total would
    # pass AGENTQ_SLEEP_BUDGET_S (default 600), else add the seconds to the job's counter file.
    # Returns 1 without deciding when the budget is not a whole number; dispatch.py decides then.
    _secs=$1
    shift
    _budget=${AGENTQ_SLEEP_BUDGET_S:-600}
    case $_budget in
        *[!0-9]*) return 1 ;;
    esac
    _dir=${AGENTQ_HOME:-${HOME:-/}/.local/state/agentq}/guard
    _state=$_dir/sleep-$AGENTQ_JOB
    _used=0
    if [ -f "$_state" ]; then
        read -r _used < "$_state" 2>/dev/null || :
        case $_used in
            ''|*[!0-9]*) _used=0 ;;
        esac
    fi
    _new=$((_used + _secs))
    if [ "$_new" -gt "$_budget" ]; then
        shim_refuse "sleep budget used ($_new s of $_budget s)" \
            "workers never babysit. Launch long work detached, write a status file, and report its PID and paths." \
            sleep "$@"
    fi
    [ -d "$_dir" ] || mkdir -p "$_dir" 2>/dev/null
    printf '%s\n' "$_new" > "$_state" 2>/dev/null
    return 0
}

shim_check_grep_path() {
    # shim_check_grep_path <path> <argv...>: refuse a recursive grep whose path is a broad root or a git checkout root.
    # A path that is not a directory (a file) passes.
    shim_resolve "$1"
    _full=$RESOLVED
    shift
    [ -n "$_full" ] || return 0
    if shim_broad_root "$_full" || [ -e "$_full/.git" ]; then
        shim_refuse "recursive grep over $BROAD_ROOT refused" \
            "use rg (skips .gitignore'd dirs like .venv and data) or git grep, or grep a specific subfolder" \
            grep "$@"
    fi
}

shim_guard_grep() {
    # Decides one grep argv. Non-recursive grep returns at once; a recursive grep is checked path by path.
    _rec=
    _pat=
    _pending=
    _dashes=
    _pos=
    _nl='
'
    for _arg
    do
        if [ -n "$_pending" ]; then
            [ "$_pending" = recurse-value ] && [ "$_arg" = recurse ] && _rec=1
            _pending=
            continue
        fi
        if [ -z "$_dashes" ]; then
            case $_arg in
                --) _dashes=1; continue ;;
                --recursive|--dereference-recursive|--directories=recurse) _rec=1; continue ;;
                --directories) _pending=recurse-value; continue ;;
                --regexp=*|--file=*) _pat=1; continue ;;
                --regexp|--file) _pat=1; _pending=other; continue ;;
                --max-count|--after-context|--before-context|--context|--devices|--label|--binary-files|--exclude|--include|--exclude-dir|--exclude-from|--group-separator)
                    _pending=other; continue ;;
                --*) continue ;;
                -?*)
                    _flags=${_arg#-}
                    while [ -n "$_flags" ]; do
                        _c=${_flags%"${_flags#?}"}
                        _flags=${_flags#?}
                        case $_c in
                            r|R) _rec=1 ;;
                            e|f)
                                _pat=1
                                [ -z "$_flags" ] && _pending=other
                                break ;;
                            d)
                                if [ -z "$_flags" ]; then
                                    _pending=recurse-value
                                elif [ "$_flags" = recurse ]; then
                                    _rec=1
                                fi
                                break ;;
                            m|A|B|C|D)
                                [ -z "$_flags" ] && _pending=other
                                break ;;
                        esac
                    done
                    continue ;;
            esac
        fi
        _pos="$_pos$_arg$_nl"
    done
    [ -n "$_rec" ] || return 0
    if [ -z "$_pat" ]; then
        _pos=${_pos#*"$_nl"}
    fi
    [ -n "$_pos" ] || _pos=.$_nl
    while IFS= read -r _path; do
        [ -n "$_path" ] || continue
        shim_check_grep_path "$_path" "$@"
    done <<GREP_PATHS
$_pos
GREP_PATHS
}
