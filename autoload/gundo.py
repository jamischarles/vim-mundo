# vim: set fdm=marker ts=4 sw=4 et:
# ============================================================================
# File:        gundo.py
# Description: vim global plugin to visualize your undo tree
# Maintainer:  Steve Losh <steve@stevelosh.com>
# License:     GPLv2+ -- look it up.
# Notes:       Much of this code was thiefed from Mercurial, and the rest was
#              heavily inspired by scratch.vim and histwin.vim.
#
# ============================================================================

import difflib
import itertools
import re
import sys
import time
import vim
import tempfile

# Mercurial's graphlog code --------------------------------------------------------#{{{
def asciiedges(seen, rev, parents):
    """adds edge info to changelog DAG walk suitable for ascii()"""
    if rev not in seen:
        seen.append(rev)
    nodeidx = seen.index(rev)

    knownparents = []
    newparents = []
    for parent in parents:
        if parent in seen:
            knownparents.append(parent)
        else:
            newparents.append(parent)

    ncols = len(seen)
    seen[nodeidx:nodeidx + 1] = newparents
    edges = [(nodeidx, seen.index(p)) for p in knownparents]

    if len(newparents) > 0:
        edges.append((nodeidx, nodeidx))
    if len(newparents) > 1:
        edges.append((nodeidx, nodeidx + 1))

    nmorecols = len(seen) - ncols
    return nodeidx, edges, ncols, nmorecols

def get_nodeline_edges_tail(
        node_index, p_node_index, n_columns, n_columns_diff, p_diff, fix_tail):
    if fix_tail and n_columns_diff == p_diff and n_columns_diff != 0:
        # Still going in the same non-vertical direction.
        if n_columns_diff == -1:
            start = max(node_index + 1, p_node_index)
            tail = ["|", " "] * (start - node_index - 1)
            tail.extend(["/", " "] * (n_columns - start))
            return tail
        else:
            return ["\\", " "] * (n_columns - node_index - 1)
    else:
        return ["|", " "] * (n_columns - node_index - 1)

def draw_edges(edges, nodeline, interline):
    for (start, end) in edges:
        if start == end + 1:
            interline[2 * end + 1] = "/"
        elif start == end - 1:
            interline[2 * start + 1] = "\\"
        elif start == end:
            interline[2 * start] = "|"
        else:
            nodeline[2 * end] = "+"
            if start > end:
                (start, end) = (end, start)
            for i in range(2 * start + 1, 2 * end):
                if nodeline[i] != "+":
                    nodeline[i] = "-"

def fix_long_right_edges(edges):
    for (i, (start, end)) in enumerate(edges):
        if end > start:
            edges[i] = (start, end + 1)

def ascii(buf, state, type, char, text, coldata, verbose):
    """prints an ASCII graph of the DAG

    takes the following arguments (one call per node in the graph):

      - Somewhere to keep the needed state in (init to asciistate())
      - Column of the current node in the set of ongoing edges.
      - Type indicator of node data == ASCIIDATA.
      - Payload: (char, lines):
        - Character to use as node's symbol.
        - List of lines to display as the node's text.
      - Edges; a list of (col, next_col) indicating the edges between
        the current node and its parents.
      - Number of columns (ongoing edges) in the current revision.
      - The difference between the number of columns (ongoing edges)
        in the next revision and the number of columns (ongoing edges)
        in the current revision. That is: -1 means one column removed;
        0 means no columns added or removed; 1 means one column added.
      - Verbosity: if enabled then the graph prints an extra '|' 
        between each line of information.
    """

    idx, edges, ncols, coldiff = coldata
    assert -2 < coldiff < 2
    if coldiff == -1:
        # Transform
        #
        #     | | |        | | |
        #     o | |  into  o---+
        #     |X /         |/ /
        #     | |          | |
        fix_long_right_edges(edges)

    # add_padding_line says whether to rewrite
    #
    #     | | | |        | | | |
    #     | o---+  into  | o---+
    #     |  / /         |   | |  # <--- padding line
    #     o | |          |  / /
    #                    o | |
    add_padding_line = (len(text) > 2 and coldiff == -1 and
                        [x for (x, y) in edges if x + 1 < y] and
                        verbose)

    # fix_nodeline_tail says whether to rewrite
    #
    #     | | o | |        | | o | |
    #     | | |/ /         | | |/ /
    #     | o | |    into  | o / /   # <--- fixed nodeline tail
    #     | |/ /           | |/ /
    #     o | |            o | |
    fix_nodeline_tail = len(text) <= 2 and not add_padding_line

    # nodeline is the line containing the node character (typically o)
    nodeline = ["|", " "] * idx
    nodeline.extend([char, " "])

    nodeline.extend(
        get_nodeline_edges_tail(idx, state[1], ncols, coldiff,
                                state[0], fix_nodeline_tail))

    # shift_interline is the line containing the non-vertical
    # edges between this entry and the next
    shift_interline = ["|", " "] * idx
    if coldiff == -1:
        n_spaces = 1
        edge_ch = "/"
    elif coldiff == 0:
        n_spaces = 2
        edge_ch = "|"
    else:
        n_spaces = 3
        edge_ch = "\\"
    shift_interline.extend(n_spaces * [" "])
    shift_interline.extend([edge_ch, " "] * (ncols - idx - 1))

    # draw edges from the current node to its parents
    draw_edges(edges, nodeline, shift_interline)

    # lines is the list of all graph lines to print
    lines = [nodeline]
    if add_padding_line:
        lines.append(get_padding_line(idx, ncols, edges))
    lines.append(shift_interline)

    # make sure that there are as many graph lines as there are
    # log strings
    if any("/" in s for s in lines) or verbose:
        while len(text) < len(lines):
            text.append('')
    if len(lines) < len(text):
        extra_interline = ["|", " "] * (ncols + coldiff)
        while len(lines) < len(text):
            lines.append(extra_interline)

    indentation_level = max(ncols, ncols + coldiff)
    for (line, logstr) in zip(lines, text):
        ln = "%-*s %s" % (2 * indentation_level, "".join(line), logstr)
        buf.write(ln.rstrip() + '\n')

    # ... and start over
    state[0] = coldiff
    state[1] = idx

def generate(dag, edgefn, current, verbose):
    seen, state = [], [0, 0]
    buf = Buffer()
    for node, parents in list(dag):
        if node.time:
            age_label = age(int(node.time))
        else:
            age_label = 'Original'
        line = '[%s] %s' % (node.n, age_label)
        if node.n == current:
            char = '@'
        elif node.saved:
            char = 'w'
        else:
            char = 'o'
        ascii(buf, state, 'C', char, [line], edgefn(seen, node, parents), verbose)
    return buf.b


# Mercurial age function -----------------------------------------------------------
agescales = [("year", 3600 * 24 * 365),
             ("month", 3600 * 24 * 30),
             ("week", 3600 * 24 * 7),
             ("day", 3600 * 24),
             ("hour", 3600),
             ("minute", 60),
             ("second", 1)]

def age(ts):
    '''turn a timestamp into an age string.'''

    def plural(t, c):
        if c == 1:
            return t
        return t + "s"
    def fmt(t, c):
        return "%d %s" % (c, plural(t, c))

    now = time.time()
    then = ts
    if then > now:
        return 'in the future'

    delta = max(1, int(now - then))
    if delta > agescales[0][1] * 2:
        return time.strftime('%Y-%m-%d', time.gmtime(float(ts)))

    for t, s in agescales:
        n = delta // s
        if n >= 2 or s == 1:
            return '%s ago' % fmt(t, n)

#}}}
# Python Vim utility functions -----------------------------------------------------#{{{
normal = lambda s: vim.command('normal %s' % s)
normal_silent = lambda s: vim.command('silent! normal %s' % s)

MISSING_BUFFER = "Cannot find Gundo's target buffer (%s)"
MISSING_WINDOW = "Cannot find window (%s) for Gundo's target buffer (%s)"

def _check_sanity():
    '''Check to make sure we're not crazy.

    Does the following things:

        * Make sure the target buffer still exists.
    '''
    b = int(vim.eval('g:gundo_target_n'))

    if not vim.eval('bufloaded(%d)' % b):
        vim.command('echo "%s"' % (MISSING_BUFFER % b))
        return False

    w = int(vim.eval('bufwinnr(%d)' % b))
    if w == -1:
        vim.command('echo "%s"' % (MISSING_WINDOW % (w, b)))
        return False

    return True

def _goto_window_for_buffer(b):
    w = int(vim.eval('bufwinnr(%d)' % int(b)))
    vim.command('%dwincmd w' % w)

def _goto_window_for_buffer_name(bn):
    b = vim.eval('bufnr("%s")' % bn)
    return _goto_window_for_buffer(b)

def _undo_to(n):
    n = int(n)
    if n == 0:
        vim.command('silent earlier %s' % (int(vim.eval('&undolevels')) + 1))
    else:
        vim.command('silent undo %d' % n)


INLINE_HELP = '''\
" Gundo for %s (%d):
" j/k  - Next/Prev undo state.
" J/K  - Next/Prev write state.
" /    - Find changes that match string.
" n/N  - Next/Prev undo that matches search.
" P    - Play current state to selected undo.
" d    - Vert diff of undo with current state.
" p    - Diff of selected undo and current state.
" r    - Diff of selected undo and prior undo.
" q    - Quit!
" <cr> - Revert to selected state.

'''
INLINE_HELP_LINES = len(INLINE_HELP.split('\n'))
#}}}
# Python undo tree data structures and functions -----------------------------------#{{{
class Buffer(object):
    def __init__(self):
        self.b = ''

    def write(self, s):
        self.b += s

class Node(object):
    def __init__(self, n, parent, time, curhead, saved):
        self.n = int(n)
        self.parent = parent
        self.children = []
        self.curhead = curhead
        self.saved = saved
        self.time = time

    def __repr__(self):
        return "[n=%s,parent=%s,time=%s,curhead=%s,saved=%s]" % \
            (self.n,self.parent,self.time,self.curhead,self.saved)

def _make_nodes(alts, nodes, parent=None):
    p = parent

    for alt in alts:
        if alt:
            curhead = 'curhead' in alt
            saved = 'save' in alt
            node = Node(n=alt['seq'], parent=p, time=alt['time'], curhead=curhead, saved=saved)
            nodes.append(node)
            if alt.get('alt'):
                _make_nodes(alt['alt'], nodes, p)
            p = node

def make_nodes():
    _goto_window_for_buffer(vim.eval('g:gundo_target_n'))
    ut = vim.eval('undotree()')
    entries = ut['entries']

    root = Node(0, None, False, 0, 0)
    nodes = []
    _make_nodes(entries, nodes, root)
    nodes.append(root)
    nmap = dict((node.n, node) for node in nodes)
    return nodes, nmap

def changenr(nodes):
    """ Return the number of the most recent change. """
    _curhead_l = list(itertools.dropwhile(lambda n: not n.curhead, nodes))
    if _curhead_l:
        current = _curhead_l[0].parent.n
    else:
        current = int(vim.eval('changenr()'))
    return current

#}}}
# Gundo rendering ------------------------------------------------------------------#{{{

# Rendering utility functions
def _fmt_time(t):
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(float(t)))

def _output_preview_text(lines):
    _goto_window_for_buffer_name('__Gundo_Preview__')
    vim.command('setlocal modifiable')
    vim.current.buffer[:] = [line.rstrip() for line in lines]
    vim.command('setlocal nomodifiable')

def _generate_preview_diff(current, node_before, node_after):
    _goto_window_for_buffer(vim.eval('g:gundo_target_n'))

    if not node_after.n:    # we're at the original file
        before_lines = []

        _undo_to(0)
        after_lines = vim.current.buffer[:]

        before_name = 'n/a'
        before_time = ''
        after_name = 'Original'
        after_time = ''
    elif not node_before.n: # we're at a pseudo-root state
        _undo_to(0)
        before_lines = vim.current.buffer[:]

        _undo_to(node_after.n)
        after_lines = vim.current.buffer[:]

        before_name = 'Original'
        before_time = ''
        after_name = node_after.n
        after_time = _fmt_time(node_after.time)
    else:
        _undo_to(node_before.n)
        before_lines = vim.current.buffer[:]

        _undo_to(node_after.n)
        after_lines = vim.current.buffer[:]

        before_name = node_before.n
        before_time = _fmt_time(node_before.time)
        after_name = node_after.n
        after_time = _fmt_time(node_after.time)

    _undo_to(current)

    return list(difflib.unified_diff(before_lines, after_lines,
                                     before_name, after_name,
                                     before_time, after_time))

def _generate_change_preview_diff(current, node_before, node_after):
    _goto_window_for_buffer(vim.eval('g:gundo_target_n'))

    _undo_to(node_before.n)
    before_lines = vim.current.buffer[:]

    _undo_to(node_after.n)
    after_lines = vim.current.buffer[:]

    before_name = node_before.n or 'Original'
    before_time = node_before.time and _fmt_time(node_before.time) or ''
    after_name = node_after.n or 'Original'
    after_time = node_after.time and _fmt_time(node_after.time) or ''

    _undo_to(current)

    return list(difflib.unified_diff(before_lines, after_lines,
                                     before_name, after_name,
                                     before_time, after_time))
#}}}

def GundoRenderGraph():
    if not _check_sanity():
        return

    nodes, nmap = make_nodes()

    for node in nodes:
        node.children = [n for n in nodes if n.parent == node]

    def walk_nodes(nodes):
        for node in nodes:
            if node.parent:
                yield (node, [node.parent])
            else:
                yield (node, [])

    dag = sorted(nodes, key=lambda n: int(n.n), reverse=True)
    current = changenr(nodes)

    verbose = vim.eval('g:gundo_verbose_graph') == 1
    result = generate(walk_nodes(dag), asciiedges, current, verbose).rstrip().splitlines()
    result = [' ' + l for l in result]

    target = (vim.eval('g:gundo_target_f'), int(vim.eval('g:gundo_target_n')))
    mappings = (vim.eval('g:gundo_map_move_older'),
                vim.eval('g:gundo_map_move_newer'))

    if int(vim.eval('g:gundo_help')):
        header = (INLINE_HELP % (target + mappings)).splitlines()
    else:
        header = []

    vim.command('call s:GundoOpenGraph()')
    vim.command('setlocal modifiable')
    vim.current.buffer[:] = (header + result)
    vim.command('setlocal nomodifiable')

    i = 1
    for line in result:
        try:
            line.split('[')[0].index('@')
            i += 1
            break
        except ValueError:
            pass
        i += 1
    vim.command('%d' % (i+len(header)-1))

def GundoRenderPreview():
    if not _check_sanity():
        return

    target_state = GundoGetTargetState()
    # Check that there's an undo state. There may not be if we're talking about
    # a buffer with no changes yet.
    if target_state == None:
        _goto_window_for_buffer_name('__Gundo__')
        return
    else:
        target_state = int(target_state)

    _goto_window_for_buffer(vim.eval('g:gundo_target_n'))

    nodes, nmap = make_nodes()
    current = changenr(nodes)

    node_after = nmap[target_state]
    node_before = node_after.parent

    vim.command('call s:GundoOpenPreview()')
    _output_preview_text(_generate_preview_diff(current, node_before, node_after))

    _goto_window_for_buffer_name('__Gundo__')

def GundoGetTargetState():
    """ Get the current undo number that gundo is at.  """
    _goto_window_for_buffer_name('__Gundo__')
    target_line = vim.eval("getline('.')")
    return int(re.match('^.* \[([0-9]+)\] .*$',target_line).group(1))

def GetNextLine(direction,move_count,write,start="line('.')"):
    start_line_no = int(vim.eval(start))
    start_line = vim.eval(start)
    gundo_verbose_graph = vim.eval('g:gundo_verbose_graph')
    if gundo_verbose_graph != "0":
        distance = 2

        # If we're in between two nodes we move by one less to get back on track.
        if start_line.find('[') == -1:
            distance = distance - 1
    else:
      distance = 1
      nextline = vim.eval("getline(%d)" % (start_line_no+direction))
      idx1 = nextline.find(' @ ')
      idx2 = nextline.find(' o ')
      idx3 = nextline.find(' w ')
      # if the next line is not a revision - then go down one more.
      if (idx1+idx2+idx3) == -3:
          distance = distance + 1

    next_line = start_line_no + distance*direction
    if move_count > 1:
        return GetNextLine(direction,move_count-1,write,str(next_line))
    elif write:
        newline = vim.eval("getline(%d)" % (next_line))
        if newline.find(' w ') == -1:
            # make sure that if we can't go up/down anymore that we quit out.
            if direction < 0 and next_line == 1:
                return next_line
            if direction > 0 and next_line >= len(vim.current.window.buffer):
                return next_line
            return GetNextLine(direction,1,write,str(next_line))
    return next_line

def GundoMove(direction,move_count=1,relative=True,write=False):
    """
    Move within the undo graph in the direction specified (or to the specific
    undo node specified).

    Parameters:

      direction  - -1/1 (up/down). when 'relative' if False, the undo node to
                   move to.
      move_count - how many times to perform the operation (irrelevent for
                   relative == False). 
      relative   - whether to move up/down, or to jump to a specific undo node.

      write      - If True, move to the next written undo.
    """
    if relative:
        target_n = GetNextLine(direction,move_count,write)
    else:
        updown = 1
        if GundoGetTargetState() < direction:
            updown = -1
        target_n = GetNextLine(updown,abs(GundoGetTargetState()-direction),write)

    # Bound the movement to the graph.
    if target_n <= INLINE_HELP_LINES - 1:
        vim.command("call cursor(%d, 0)" % INLINE_HELP_LINES)
    else:
        vim.command("call cursor(%d, 0)" % target_n)

    line = vim.eval("getline('.')")

    # Move to the node, whether it's an @, o, or w
    idx1 = line.find(' @ ')
    idx2 = line.find(' o ')
    idx3 = line.find(' w ')
    idxs = []
    if idx1 != -1:
        idxs.append(idx1)
    if idx2 != -1:
        idxs.append(idx2)
    if idx3 != -1:
        idxs.append(idx3)
    minidx = min(idxs)
    if idx1 == minidx:
        vim.command("call cursor(0, %d + 2)" % idx1)
    elif idx2 == minidx:
        vim.command("call cursor(0, %d + 2)" % idx2)
    else:
        vim.command("call cursor(0, %d + 2)" % idx3)

    if vim.eval('g:gundo_auto_preview') == '1':
        GundoRenderPreview()

def GundoSearch():
    search = vim.eval("input('/')");
    vim.command("let @/='%s'"% search.replace("'","\\'"))
    GundoNextMatch()

def GundoPrevMatch():
    GundoMatch(-1)

def GundoNextMatch():
    GundoMatch(1)

def GundoMatch(down):
    """ Jump to the next node that matches the current pattern.  If there is a
    next node, search from the next node to the end of the list of changes. Stop
    on a match. """
    if not _check_sanity():
        return

    # save the current window number (should be the navigation window)
    # then generate the undo nodes, and then go back to the current window.
    _goto_window_for_buffer(vim.eval('g:gundo_target_n'))

    nodes, nmap = make_nodes()
    total = len(nodes) - 1
    orig_changenr = changenr(nodes)

    _goto_window_for_buffer_name('__Gundo__')
    curline = int(vim.eval("line('.')")) 
    gundo_node = GundoGetTargetState()

    found_version = -1
    if total > 0:
        therange = range(gundo_node-1,-1,-1)
        if down < 0:
            therange = range(gundo_node+1,total+1)
        for version in therange:
            _goto_window_for_buffer_name('__Gundo__')
            before = Node(0, None, False, 0, 0)
            if version-1 >= 0:
                before = nmap[version-1]
            undochanges = _generate_preview_diff(orig_changenr, before, nmap[version])
            # Look thru all of the changes, ignore the first two b/c those are the
            # diff timestamp fields (not relevent):
            for change in undochanges[3:]:
                match_index = vim.eval('match("%s",@/)'% change.replace('"','\\"'))
                # only consider the matches that are actual additions or
                # subtractions
                if int(match_index) >= 0 and (change.startswith('-') or change.startswith('+')):
                    found_version = version
                    break
            # found something, lets get out of here:
            if found_version != -1:
                break
    _goto_window_for_buffer_name('__Gundo__')
    if found_version >= 0:
        GundoMove(found_version,1,False)

def GundoRenderPatchdiff():
    """ Call GundoRenderChangePreview and display a vert diffpatch with the
    current file. """
    if GundoRenderChangePreview():
        # if there are no lines, do nothing (show a warning).
        _goto_window_for_buffer_name('__Gundo_Preview__')
        if vim.current.buffer[:] == ['']:
            # restore the cursor position before exiting.
            _goto_window_for_buffer_name('__Gundo__')
            vim.command('unsilent echo "No difference between current file and undo number!"')
            return False

        # quit out of gundo main screen
        _goto_window_for_buffer_name('__Gundo__')
        vim.command('quit')

        # save the __Gundo_Preview__ buffer to a temp file.
        _goto_window_for_buffer_name('__Gundo_Preview__')
        (handle,filename) = tempfile.mkstemp()
        vim.command('silent! w %s' % (filename))
        # exit the __Gundo_Preview__ window
        vim.command('quit')
        # diff the temp file
        vim.command('silent! keepalt vert diffpatch %s' % (filename))
        vim.command('set buftype=nofile')
        return True
    return False

def GundoGetChangesForLine(current,nodesandnmap=make_nodes()):
    if not _check_sanity():
        return False

    target_state = GundoGetTargetState()

    # Check that there's an undo state. There may not be if we're talking about
    # a buffer with no changes yet.
    if target_state == None:
        _goto_window_for_buffer_name('__Gundo__')
        return False
    else:
        target_state = int(target_state)

    _goto_window_for_buffer(vim.eval('g:gundo_target_n'))

    nodes, nmap = nodesandnmap

    node_after = nmap[target_state]
    node_before = nmap[current]
    return _generate_change_preview_diff(current, node_before, node_after)

def GundoRenderChangePreview():
    """ Render the selected undo level with the current file.
    Return True on success, False on failure. """

    nodes, nmap = make_nodes()
    current = changenr(nodes)

    vim.command('call s:GundoOpenPreview()')
    _output_preview_text(GundoGetChangesForLine(current,(nodes,nmap)))

    _goto_window_for_buffer_name('__Gundo__')

    return True


# Gundo undo/redo
def GundoRevert():
    if not _check_sanity():
        return

    target_n = GundoGetTargetState()
    back = vim.eval('g:gundo_target_n')

    _goto_window_for_buffer(back)
    _undo_to(target_n)

    vim.command('GundoRenderGraph')
    _goto_window_for_buffer(back)

    if int(vim.eval('g:gundo_close_on_revert')):
        vim.command('GundoToggle')

def GundoPlayTo():
    if not _check_sanity():
        return

    target_n = GundoGetTargetState()
    back = int(vim.eval('g:gundo_target_n'))
    delay = int(vim.eval('g:gundo_playback_delay'))

    vim.command('echo "%s"' % back)

    _goto_window_for_buffer(back)
    normal('zR')

    nodes, nmap = make_nodes()

    start = nmap[changenr(nodes)]
    end = nmap[target_n]

    def _walk_branch(origin, dest):
        rev = origin.n < dest.n

        nodes = []
        if origin.n > dest.n:
            current, final = origin, dest
        else:
            current, final = dest, origin

        while current.n >= final.n:
            if current.n == final.n:
                break
            nodes.append(current)
            current = current.parent
        else:
            return None
        nodes.append(current)

        if rev:
            return reversed(nodes)
        else:
            return nodes

    branch = _walk_branch(start, end)

    if not branch:
        vim.command('unsilent echo "No path to that node from here!"')
        return

    for node in branch:
        _undo_to(node.n)
        vim.command('GundoRenderGraph')
        normal('zz')
        _goto_window_for_buffer(back)
        vim.command('redraw')
        vim.command('sleep %dm' % delay)

def initPythonModule():
    if sys.version_info[:2] < (2, 4):
        vim.command('let s:has_supported_python = 0')
