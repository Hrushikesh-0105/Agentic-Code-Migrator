% Octave / Matlab side of the differential harness (sandbox/polyglot.py); run once per case:
%     octave --eval "acm_harness"   with ACM_CASES=<JSON-lines file> ACM_CASE=<index>, the code on the path
% Calls the function with feval inside evalc (captures printed output), asking for as many outputs as the call
% says (default: what nargout(fn) reports, at least 1 when it returns anything). Prints SENTINEL + the result in
% runner.py's type-tagged format. Byte arguments arrive as {"t": "u8", "v": [ints]} (the host converts them).
1;

function s = acm_q(x)
  s = jsonencode(char(x));
end

function s = acm_num(x)
  if isinteger(x)
    s = sprintf('{"t": "int", "v": "%d"}', x);
  elseif islogical(x)
    if x, s = '{"t": "bool", "v": true}'; else, s = '{"t": "bool", "v": false}'; end
  elseif isnan(x)
    s = '{"t": "float", "v": "nan"}';
  elseif isinf(x)
    if x > 0, s = '{"t": "float", "v": "inf"}'; else, s = '{"t": "float", "v": "-inf"}'; end
  elseif ~isreal(x)
    s = sprintf('{"t": "repr", "v": "%s"}', num2str(x));
  else
    s = sprintf('{"t": "float", "v": %.17g}', double(x));
  end
end

function s = acm_enc(v, depth)
  if nargin < 2, depth = 0; end
  if depth > 30, s = '{"t": "repr", "v": "<deep>"}'; return; end
  if ischar(v)
    if size(v, 1) <= 1
      s = sprintf('{"t": "text", "v": %s}', acm_q(v));
    else
      parts = arrayfun(@(i) sprintf('{"t": "text", "v": %s}', acm_q(v(i, :))), 1:size(v, 1), 'UniformOutput', false);
      s = ['{"t": "list", "v": [' strjoin(parts, ', ') ']}'];
    end
  elseif isa(v, 'string')
    s = sprintf('{"t": "text", "v": %s}', acm_q(char(v)));
  elseif isa(v, 'function_handle')
    s = sprintf('{"t": "repr", "v": %s}', acm_q(func2str(v)));
  elseif isa(v, 'containers.Map')
    ks = keys(v); parts = {};
    for i = 1:numel(ks)
      parts{end + 1} = ['[' acm_enc(ks{i}, depth + 1) ', ' acm_enc(v(ks{i}), depth + 1) ']'];
    end
    s = ['{"t": "dict", "v": [' strjoin(parts, ', ') ']}'];
  elseif iscell(v)
    parts = cellfun(@(x) acm_enc(x, depth + 1), v(:)', 'UniformOutput', false);
    s = ['{"t": "list", "v": [' strjoin(parts, ', ') ']}'];
  elseif isstruct(v)
    if numel(v) ~= 1
      parts = arrayfun(@(x) acm_enc(x, depth + 1), v(:)', 'UniformOutput', false);
      s = ['{"t": "list", "v": [' strjoin(parts, ', ') ']}'];
      return;
    end
    f = fieldnames(v); parts = {};
    for i = 1:numel(f)
      parts{end + 1} = ['[{"t": "text", "v": ' acm_q(f{i}) '}, ' acm_enc(v.(f{i}), depth + 1) ']'];
    end
    s = ['{"t": "dict", "v": [' strjoin(parts, ', ') ']}'];
  elseif isnumeric(v) || islogical(v)
    if isempty(v)
      s = '{"t": "list", "v": []}';
    elseif isscalar(v)
      s = acm_num(v);
    elseif isvector(v)
      parts = arrayfun(@(x) acm_num(x), v(:)', 'UniformOutput', false);
      s = ['{"t": "list", "v": [' strjoin(parts, ', ') ']}'];
    else
      rows = {};
      for r = 1:size(v, 1)
        rows{end + 1} = acm_enc(v(r, :), depth + 1);
      end
      s = ['{"t": "list", "v": [' strjoin(rows, ', ') ']}'];
    end
  elseif isobject(v)
    p = properties(v); parts = {};
    for i = 1:numel(p)
      parts{end + 1} = ['[{"t": "text", "v": ' acm_q(p{i}) '}, ' acm_enc(v.(p{i}), depth + 1) ']'];
    end
    s = ['{"t": "object", "class": ' acm_q(class(v)) ', "v": {"t": "dict", "v": [' strjoin(parts, ', ') ']}}'];
  else
    s = sprintf('{"t": "repr", "v": %s}', acm_q(class(v)));
  end
end

function items = acm_items(x)
  % jsondecode gives a struct array (same fields) or a cell array (mixed): both -> a cell row
  if iscell(x)
    items = x(:)';
  elseif isstruct(x)
    items = arrayfun(@(e) e, x(:)', 'UniformOutput', false);
  elseif isempty(x)
    items = {};
  else
    items = num2cell(x(:)');
  end
end

function v = acm_dec(t)
  if ~isstruct(t), v = t; return; end
  if ~isfield(t, 'v'), val = []; else, val = t.v; end
  switch t.t
    case 'none', v = [];
    case 'bool', v = logical(val);
    case 'int', v = str2double(val);
    case 'float'
      if ischar(val)
        switch val, case 'nan', v = NaN; case 'inf', v = Inf; case '-inf', v = -Inf; otherwise, v = str2double(val); end
      else, v = double(val); end
    case {'text', 'str'}, v = char(val);
    case 'u8', v = uint8(val(:)');
    case {'list', 'tuple', 'set'}
      items = cellfun(@acm_dec, acm_items(val), 'UniformOutput', false);
      if ~isempty(items) && all(cellfun(@(x) (isnumeric(x) || islogical(x)) && isscalar(x), items))
        v = cell2mat(items);
      else
        v = items;
      end
    case 'dict'
      pairs = acm_items(val); v = struct();
      allText = true;
      for i = 1:numel(pairs)
        p = acm_items(pairs{i});
        if ~any(strcmp(p{1}.t, {'text', 'str'})), allText = false; end
      end
      if allText
        for i = 1:numel(pairs)
          p = acm_items(pairs{i}); v.(p{1}.v) = acm_dec(p{2});
        end
      else
        v = containers.Map('KeyType', 'double', 'ValueType', 'any');
        for i = 1:numel(pairs)
          p = acm_items(pairs{i}); v(acm_dec(p{1})) = acm_dec(p{2});
        end
      end
    otherwise
      error('acm:decode', 'cannot decode tag %s', t.t);
  end
end

lines = strsplit(fileread(getenv('ACM_CASES')), "\n");
lines = lines(~cellfun(@isempty, strtrim(lines)));
c = jsondecode(lines{str2double(getenv('ACM_CASE')) + 1});
call = c.call;
if isfield(call, 'args'), args = cellfun(@acm_dec, acm_items(call.args), 'UniformOutput', false); else, args = {}; end
fn = call.member;
res = {sprintf('"case_id": %s', jsonencode(c.case_id))};
out = '';
try
  if ~exist(fn)
    error('acm:unresolved', '%s is not defined', fn);
  end
  if isfield(call, 'nargout'), n = call.nargout; else, n = nargout(fn); if n < 0, n = 1; end; end
  if n == 0
    out = evalc('feval(fn, args{:});');
    value = '{"t": "none"}';
  else
    outs = cell(1, n);
    out = evalc('[outs{1:n}] = feval(fn, args{:});');
    if n == 1
      value = acm_enc(outs{1});
    else
      value = ['{"t": "tuple", "v": [' strjoin(cellfun(@(x) acm_enc(x), outs, 'UniformOutput', false), ', ') ']}'];
    end
  end
  res{end + 1} = '"status": "ok"';
  res{end + 1} = ['"value": ' value];
catch err
  if strcmp(err.identifier, 'acm:unresolved')
    res{end + 1} = '"status": "unresolved"';
    res{end + 1} = ['"message": ' acm_q(err.message)];
  else
    id = err.identifier;
    if isempty(id), id = 'error'; end
    res{end + 1} = '"status": "exception"';
    res{end + 1} = ['"exception": {"type": ' acm_q(id) ', "hierarchy": [' acm_q(id) ', "MException"], "message": ' acm_q(err.message) '}'];
  end
end
after = cellfun(@(x) acm_enc(x), args, 'UniformOutput', false);
res{end + 1} = ['"args_after": [' strjoin(after, ', ') ']'];
res{end + 1} = '"self_after": null';
res{end + 1} = ['"stdout": ' acm_q(out)];
res{end + 1} = '"stderr": ""';
printf('%s{%s}\n', '__HARNESS_RESULT__', strjoin(res, ', '));
