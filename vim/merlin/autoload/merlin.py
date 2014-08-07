import subprocess
import signal
import json
import vim
import re
import os
import sys
from itertools import groupby

import vimbufsync
vimbufsync.check_version("0.1.0",who="merlin")

flags = []
enclosing_types = [] # nothing to see here
current_enclosing = -1
atom_bound = re.compile('[a-z_0-9A-Z\'`.]')

######## ERROR MANAGEMENT

class MerlinExc(Exception):
  def __init__(self, value):
      self.value = value
  def __str__(self):
    return repr(self.value)

class Failure(MerlinExc):
  pass

class Error(MerlinExc):
  pass

class MerlinException(MerlinExc):
  pass

def try_print_error(e, msg=None):
  try:
    raise e
  except Error as e:
    if msg: print(msg)
    else: print(e.value['message'])
  except Exception as e:
    # Always print to stdout
    # vim try to be 'smart' and prepend a backtrace when writing to stderr
    # WTF?!
    if msg: print (msg)
    else:
      msg = str(e)
      if re.search('Not_found',msg):
        print ("error: Not found")
        return None
      elif re.search('Cmi_format.Error', msg):
        if vim.eval('exists("b:merlin_incompatible_version")') == '0':
          vim.command('let b:merlin_incompatible_version = 1')
          print ("The version of merlin you're using doesn't support this version of ocaml")
        return None
      print (msg)

def catch_and_print(f, msg=None):
  try:
    return f()
  except MerlinExc as e:
    try_print_error(e, msg=msg)

######## PROCESS MANAGEMENT

class MerlinProcess:
  def __init__(self):
    self.mainpipe = None
    self.saved_sync = None

  def restart(self):
    if self.mainpipe:
      try:
        try:
          self.mainpipe.terminate()
        except OSError:
          pass
        self.mainpipe.communicate()
      except OSError:
        pass
    try:
      cmd = [vim.eval("merlin#FindOcamlMerlin()"),"-ignore-sigint"]
      cmd.extend(flags)
      self.mainpipe = subprocess.Popen(
              cmd,
              stdin=subprocess.PIPE,
              stdout=subprocess.PIPE,
              stderr=None,
          )
    except OSError as e:
      print("Failed starting ocamlmerlin. Please ensure that ocamlmerlin binary\
              is executable.")
      raise e

  def command(self, *cmd):
    if self.mainpipe == None or self.mainpipe.returncode != None:
      self.restart()
    json.dump(cmd, self.mainpipe.stdin)
    line = self.mainpipe.stdout.readline()
    result = json.loads(line)
    content = None
    if len(result) == 2:
      content = result[1]

    if result[0] == "return":
      return content
    elif result[0] == "failure":
      raise Failure(content)
    elif result[0] == "error":
      raise Error(content)
    elif result[0] == "exception":
      raise MerlinException(content)

## MULTI-PROCESS
#merlin_processes = {}
#def merlin_process():
#  global merlin_processes
#  name = vim.eval("exists('b:merlin_project') ? b:merlin_project : ''")
#  if not name in merlin_processes:
#    merlin_processes[name] = MerlinProcess()
#  return merlin_processes[name]

# MONO-PROCESS
merlin_processes = None
def merlin_process():
  global merlin_processes
  if not merlin_processes:
    merlin_processes = MerlinProcess()
  return merlin_processes

def command(*cmd):
  return merlin_process().command(*cmd)

def dump(*cmd):
  print(command('dump', *cmd))

######## BASIC COMMANDS

def parse_position(pos):
  position = pos['cursor']
  marker = pos['marker']
  return (position['line'], position['col'], marker)

def display_load_failures(result):
  if 'failures' in result:
    print (result['failures'])
  return result['result']

def command_tell(content):
  if isinstance(content,list):
    content = "\n".join(content) + "\n"
  return parse_position(command("tell", "source", content))

def command_find_use(*packages):
  result = catch_and_print(lambda: command('find', 'use', packages))
  return display_load_failures(result)

def command_reset(kind="ml",name=None):
  global saved_sync
  if name: r = command("reset",kind,name)
  else:    r = command("reset",kind)
  if name == "myocamlbuild.ml":
    command_find_use("ocamlbuild")
  saved_sync = None
  return r

def command_seek(mtd,line,col):
  return parse_position(command("seek", mtd, {'line' : line, 'col': col}))

def command_complete_cursor(base,line,col):
  return command("complete", "prefix", base, "at", {'line' : line, 'col': col})

def command_locate(path, line, col):
  try:
    if line is None or col is None:
        return command("locate", path)
    else:
        pos_or_err = command("locate", path, "at", {'line': line, 'col': col})
    if not isinstance(pos_or_err, dict):
      print(pos_or_err)
    else:
      l = pos_or_err['pos']['line']
      c = pos_or_err['pos']['col']
      if "file" in pos_or_err:
        vim.command(":split %s" % pos_or_err['file'])
      vim.current.window.cursor = (l, c)
  except MerlinExc as e:
    try_print_error(e)

def command_occurrences(line, col):
  try:
    lst_or_err = command("occurrences", "ident", "at", {'line':line, 'col':col})
    if not isinstance(lst_or_err, list):
      print(lst_or_err)
    else:
      return lst_or_err
  except MerlinExc as e:
    try_print_error(e)

######## BUFFER SYNCHRONIZATION

def sync_buffer_to_(to_line, to_col, load_project=True,skip_marker=False):
  process = merlin_process()
  saved_sync = process.saved_sync
  curr_sync = vimbufsync.sync()
  cb = vim.current.buffer
  max_line = len(cb)
  end_line = min(to_line, max_line)

  if saved_sync and curr_sync.bufnr() == saved_sync.bufnr():
    line, col = min(saved_sync.pos(),(to_line,to_col))
    col = 0
    command_seek("exact", line, col)
  else:
    if load_project:
      project = vim.eval("exists('b:dotmerlin') && len(b:dotmerlin) > 0 ? b:dotmerlin[0] : ''")
      command("project","load",project)
    command_reset(
            kind=(vim.eval("expand('%:e')") == "mli") and "mli" or "ml",
            name=vim.eval("expand('%:p')")
            )
  line, col, _ = parse_position(command("tell", "start"))

  # Send prefix content
  if line <= end_line:
    rest    = cb[line-1][col:]
    content = cb[line:end_line]
    content.insert(0, rest)
    process.saved_sync = curr_sync
    command_tell(content)

  # put marker
  _, _, marker = parse_position(command("tell","marker"))

  # satisfy marker
  while marker and (end_line < max_line):
    next_end = min(max_line,end_line + 50)
    _, _, marker = command_tell(cb[end_line:next_end])
    end_line = next_end

  # put eof if marker still on stack at max_line
  if marker: command("tell","eof")
  if not skip_marker: command("seek","marker")

def sync_buffer_to(to_line, to_col, load_project=True,skip_marker=False):
  return catch_and_print(lambda: sync_buffer_to_(to_line, to_col, load_project=load_project,skip_marker=skip_marker))

def sync_buffer():
  to_line, to_col = vim.current.window.cursor
  sync_buffer_to(to_line, to_col)

def sync_full_buffer():
  sync_buffer_to(len(vim.current.buffer),0,skip_marker=True)

######## VIM FRONTEND

# Spawn a fresh new process
def vim_restart():
  merlin_process().restart()
  path = vim.eval("expand('%:p')")
  load_project(path)

# Reload changed cmi files then retype all definitions
def vim_reload():
  return command("refresh")

# Complete
def vim_complete_cursor(base, vimvar):
  vim.command("let %s = []" % vimvar)
  line, col = vim.current.window.cursor
  wspaces = re.compile("[\n ]+")
  try:
    sync_buffer()
    props = command_complete_cursor(base,line,col)
    for prop in props:
      vim.command("let l:tmp = {'word':'%s','menu':'%s','info':'%s','kind':'%s'}" %
        (prop['name'].replace("'", "''")
        ,re.sub(wspaces, " ", prop['desc']).replace("'", "''")
        ,prop['info'].replace("'", "''")
        ,prop['kind'][:1].replace("'", "''")
        ))
      vim.command("call add(%s, l:tmp)" % vimvar)
  except MerlinExc as e:
    try_print_error(e)

# Error listing
def vim_loclist(vimvar, ignore_warnings):
  vim.command("let %s = []" % vimvar)
  errors = command("errors")
  bufnr = vim.current.buffer.number
  nr = 0
  for error in errors:
    if error['type'] == 'warning' and vim.eval(ignore_warnings) == 'true':
        continue
    ty = 'w'
    if error['type'] == 'type' or error['type'] == 'parser':
      ty = 'e'
    lnum = 1
    lcol = 1
    if error.has_key('start'):
        lnum = error['start']['line']
        lcol = error['start']['col'] + 1
    vim.command("let l:tmp = {'bufnr':%d,'lnum':%d,'col':%d,'vcol':0,'nr':%d,'pattern':'','text':'%s','type':'%s','valid':1}" %
        (bufnr, lnum, lcol, nr, error['message'].replace("'", "''").replace("\n", " "), ty))
    nr = nr + 1
    vim.command("call add(%s, l:tmp)" % vimvar)

# Findlib Package
def vim_findlib_list(vimvar):
  pkgs = command('find', 'list')
  vim.command("let %s = []" % vimvar)
  for pkg in pkgs:
    vim.command("call add(%s, '%s')" % (vimvar, pkg))

def vim_findlib_use(*args):
  return command_find_use(*args)

# Locate
def vim_locate_at_cursor(path):
  line, col = vim.current.window.cursor
  sync_buffer_to(line, col)
  command_locate(path, line, col)

def vim_locate_under_cursor():
  delimiters = [' ', '\n', '=', ';', ',', '(', ')', '[', ']', '{', '}', '|', '"',"+","-","*","/" ]
  line_nb, col_nb = vim.current.window.cursor
  line = vim.current.buffer[line_nb - 1]
  start = col_nb
  stop = col_nb
  while start > 0:
    if line[start - 1] in delimiters:
        break
    else:
        start -= 1
  while stop < len(line):
    # we stop on dots because on "Foo.Ba<cursor>r.Baz.lol" I want to jump at the
    # definition of Bar, not the one of lol.
    if line[stop] in delimiters or line[stop] == '.':
        break
    else:
        stop += 1
  vim_locate_at_cursor(line[start:stop])

# Occurrences
def vim_occurrences(vimvar):
  vim.command("let %s = []" % vimvar)
  line, col = vim.current.window.cursor
  sync_full_buffer()
  lst = command_occurrences(line, col)
  lst = map(lambda x: x['start'], lst)
  bufnr = vim.current.buffer.number
  nr = 0
  cursorpos = 0
  for pos in lst:
    lnum = pos['line']
    lcol = pos['col']
    if (lnum, lcol) <= (line, col): cursorpos = nr
    vim.command("let l:tmp = {'bufnr':%d,'lnum':%d,'col':%d,'vcol':0,'nr':%d,'pattern':'','text':'occurrence','type':'I','valid':1}" %
        (bufnr, lnum, lcol + 1, nr))
    nr = nr + 1
    vim.command("call add(%s, l:tmp)" % vimvar)
  return cursorpos + 1

def vim_occurrences_replace(content):
  sync_full_buffer()
  line, col = vim.current.window.cursor
  lst = command_occurrences(line, col)
  lst.reverse()
  bufnr = vim.current.buffer.number
  nr, cursorpos = 0, 0
  for pos in lst:
    if pos['start']['line'] == pos['end']['line']:
      mlen = pos['end']['col'] - pos['start']['col']
      matcher = make_matcher(pos['start'], pos['end'])
      query = ":%s/{0}.\\{{{1}\\}}/{2}/".format(matcher,mlen,content)
      vim.command(query)

# Expression typing
def vim_type(expr,is_approx=False):
  to_line, to_col = vim.current.window.cursor
  cmd_at = ["at", {'line':to_line,'col':to_col}]
  sync_buffer_to(to_line,to_col)
  cmd_expr = ["expression", expr] if expr else []
  try:
    cmd = ["type"] + cmd_expr + cmd_at
    ty = command(*cmd)
    if isinstance(ty,dict):
      if "type" in ty: ty = ty['type']
      else: ty = str(ty)
    if is_approx: sys.stdout.write("(approx) ")
    if expr: print(expr + " : " + ty)
    else: print(ty)
  except MerlinExc as e:
    if re.search('Not_found',str(e)):
      pass
    else:
      try_print_error(e)

def bounds_of_ocaml_atom_at_pos(to_line, col):
    line = vim.current.buffer[to_line]
    start = col
    stop = col
    while start > 0:
        if atom_bound.match(line[start - 1]) is None:
            break
        else:
            start -= 1
    while stop < len(line):
        if atom_bound.match(line[stop]) is None:
            break
        else:
            stop += 1
    return (line[start:stop], start, stop)

def vim_type_enclosing(vimvar):
  global enclosing_types
  global current_enclosing
  sync_buffer()
  enclosing_types = [] # reset
  current_enclosing = -1
  to_line, to_col = vim.current.window.cursor
  pos = {'line':to_line, 'col':to_col}
  # deprecated, leave merlin compute the correct identifier
  # atom, a_start, a_end = bounds_of_ocaml_atom_at_pos(to_line - 1, to_col)
  # offset = to_col - a_start
  # arg = {'expr':atom, 'offset':offset}
  # enclosing_types = command("type", "enclosing", arg, pos)
  try:
    enclosing_types = command("type", "enclosing", "at", pos)
    if enclosing_types != []:
      vim_next_enclosing(vimvar)
    else:
      atom, _, _ = bounds_of_ocaml_atom_at_pos(to_line - 1, to_col)
      print("didn't manage to type '%s'" % atom)
  except MerlinExc as e:
    try_print_error(e)

def easy_matcher(start, stop):
  startl = ""
  startc = ""
  if start['line'] > 0:
    startl = "\%>{0}l".format(start['line'] - 1)
  if start['col'] > 0:
    startc = "\%>{0}c".format(start['col'])
  return '{0}{1}\%<{2}l\%<{3}c'.format(startl, startc, stop['line'] + 1, stop['col'] + 1)

def hard_matcher(start, stop):
  first_start = {'line' : start['line'], 'col' : start['col']}
  first_stop =  {'line' : start['line'], 'col' : 4242}
  first_line = easy_matcher(first_start, first_stop)
  mid_start = {'line' : start['line']+1, 'col' : 0}
  mid_stop =  {'line' : stop['line']-1 , 'col' : 4242}
  middle = easy_matcher(mid_start, mid_stop)
  last_start = {'line' : stop['line'], 'col' : 0}
  last_stop =  {'line' : stop['line'], 'col' : stop['col']}
  last_line = easy_matcher(last_start, last_stop)
  return "{0}\|{1}\|{2}".format(first_line, middle, last_line)

def make_matcher(start, stop):
  if start['line'] == stop['line']:
    return easy_matcher(start, stop)
  else:
    return hard_matcher(start, stop)

def enclosing_tail_info(record):
  if record['tail'] == 'call': return ' (* tail call *)'
  if record['tail'] == 'position': return ' (* tail position *)'
  return ''

def vim_next_enclosing(vimvar):
  if enclosing_types != []:
    global current_enclosing
    if current_enclosing < len(enclosing_types):
        current_enclosing += 1
    if current_enclosing < len(enclosing_types):
      tmp = enclosing_types[current_enclosing]
      matcher = make_matcher(tmp['start'], tmp['end'])
      vim.command("let {0} = matchadd('EnclosingExpr', '{1}')".format(vimvar, matcher))
      print(tmp['type'] + enclosing_tail_info(tmp))

def vim_prev_enclosing(vimvar):
  if enclosing_types != []:
    global current_enclosing
    if current_enclosing >= 0:
      current_enclosing -= 1
    if current_enclosing >= 0:
      tmp = enclosing_types[current_enclosing]
      matcher = make_matcher(tmp['start'], tmp['end'])
      vim.command("let {0} = matchadd('EnclosingExpr', '{1}')".format(vimvar, matcher))
      print(tmp['type'] + enclosing_tail_info(tmp))

# Finding files
def vim_which(name,ext):
  if isinstance(ext, list):
    name = map(lambda ext: name + "." + ext, ext)
  elif ext:
    name = name + "." + ext
  return command('which','path',name)

def vim_which_ext(ext,vimvar):
  files = command('which', 'with_ext', ext)
  vim.command("let %s = []" % vimvar)
  for f in sorted(set(files)):
    vim.command("call add(%s, '%s')" % (vimvar, f))

# Extension management
def vim_ext(enable, exts):
  state = enable and 'enable' or 'disable'
  catch_and_print(lambda: command('extension', state, exts))

def vim_ext_list(vimvar,enabled=None):
  if enabled == None:
    exts = command('extension','list')
  elif enabled:
    exts = command('extension','list','enabled')
  else:
    exts = command('extension','list','disabled')
  vim.command("let %s = []" % vimvar)
  for ext in exts:
    vim.command("call add(%s, '%s')" % (vimvar, ext))

# Custom flag selection
def vim_clear_flags():
  global flags
  flags = []
  vim_restart()

def vim_add_flags(*args):
  flags.extend(args)
  vim_restart()

def vim_selectphrase(l1,c1,l2,c2):
  # In some context, vim set column of '> to 2147483647 (2^31 - 1)
  # This cause the merlin json parser on 32 bit platforms to overflow
  bound = 2147483647 - 1
  vl1 = min(bound,int(vim.eval(l1)))
  vc1 = min(bound,int(vim.eval(c1)))
  vl2 = min(bound,int(vim.eval(l2)))
  vc2 = min(bound,int(vim.eval(c2)))
  sync_buffer_to(vl2,vc2)
  command_seek_exact(vl2,vc2)
  loc2 = command("boundary")
  if vl2 != vl1 or vc2 != vc1:
    command_seek_exact(vl1,vc1)
    loc1 = command("boundary")
  else:
    loc1 = None

  if loc2 == None:
    return

  vl1 = loc2[0]['line']
  vc1 = loc2[0]['col']
  vl2 = loc2[1]['line']
  vc2 = loc2[1]['col']
  if loc1 != None:
    vl1 = min(loc1[0]['line'], vl1)
    vc1 = min(loc1[0]['col'], vc1)
    vl2 = max(loc1[1]['line'], vl2)
    vc2 = max(loc1[1]['col'], vc2)
  for (var,val) in [(l1,vl1),(l2,vl2),(c1,vc1),(c2,vc2)]:
    vim.command("let %s = %d" % (var,val))

def load_project(directory):
  failures = catch_and_print(lambda: command("project","find",directory))
  if failures != None:
    fnames = display_load_failures(failures)
    if isinstance(fnames, list):
      vim.command('let b:dotmerlin=[%s]' % ','.join(map(lambda fname: '"'+fname+'"', fnames)))
    sync_buffer_to(1, 0, load_project=False)
