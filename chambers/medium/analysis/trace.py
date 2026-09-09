import json, binascii, collections, sys
d = json.load(open('VC100.vi.json'))
wires = {w['id']: w for w in d['wires']}
def walk(node, parent=None):
    if isinstance(node, dict):
        yield node, parent
        for k, v in node.items():
            if k in ('body','frames','nodes','children','diagram','cases','tunnels','selector'):
                yield from walk(v, node if 'id' in node else parent)
    elif isinstance(node, list):
        for x in node: yield from walk(x, parent)
nodes, parent_of = {}, {}
for n, p in walk(d['diagram']):
    if 'id' in n:
        nodes[n['id']] = n
        parent_of[n['id']] = p['id'] if p else None
term_owner = {}
for nid, n in nodes.items():
    for t in n.get('terminals', []) or []:
        term_owner[t['name']] = (nid, t)
    # tunnels
    for t in n.get('tunnels', []) or []:
        for side in ('outer','inner','inside','outside'):
            for tt in (t.get(side) or []):
                if isinstance(tt, dict) and 'name' in tt: term_owner[tt['name']] = (nid, tt)

def dec(hexstr):
    try:
        b = binascii.unhexlify(hexstr)
        # LabVIEW string const: 4-byte len + bytes
        if len(b) >= 4 and int.from_bytes(b[:4],'big') == len(b)-4:
            return b[4:].decode('latin1')
        return b.decode('latin1', 'replace')
    except Exception:
        return hexstr

def source_of(wire_id):
    w = wires.get(wire_id)
    if not w: return None
    return w['from']['node'], w['from']['terminal']

def resolve(term, depth=0):
    """Return a string describing the value feeding this input terminal."""
    if depth > 12: return '…'
    if 'const' in term:
        return repr(dec(term['const'])) if term.get('type') == 'String' else f"<{term.get('type')}:{term['const']}>"
    wid = term.get('wire')
    if not wid: return '<unwired>'
    src = source_of(wid)
    if not src: return f'<wire {wid}?>'
    nid, tname = src
    n = nodes.get(nid)
    if n is None: return f'<node {nid}?>'
    cls, op = n.get('class'), n.get('op', '')
    if cls == 'Constant':
        v = n.get('value','')
        return repr(dec(v)) if n.get('type','').startswith('String') or len(v) > 8 else f"<{n.get('type')}:{v}>"
    if cls == 'gRef':
        return f"REF[{n.get('param_idx')}]"
    if op == 'Concatenate Strings':
        ins = [t for t in n['terminals'] if t['dir']=='in']
        return ' + '.join(resolve(t, depth+1) for t in ins)
    if op in ('Number To Decimal String',):
        ins = [t for t in n['terminals'] if t['dir']=='in']
        return 'str(' + ', '.join(resolve(t, depth+1) for t in ins) + ')'
    if op == 'Index Array':
        ins = [t for t in n['terminals'] if t['dir']=='in']
        return 'Index(' + ', '.join(resolve(t, depth+1) for t in ins) + ')'
    if op == 'Build Array':
        ins = [t for t in n['terminals'] if t['dir']=='in']
        return '[' + ', '.join(resolve(t, depth+1) for t in ins) + ']'
    if op == 'Select':
        ins = [t for t in n['terminals'] if t['dir']=='in']
        return 'Select(' + ', '.join(resolve(t, depth+1) for t in ins) + ')'
    if cls in ('case','select','loop','flat_sequence') or 'tunnel' in str(tname).lower():
        # crossing a structure border: find the tunnel's other side
        for t in n.get('tunnels', []) or []:
            names = {tt.get('name') for side in ('outer','inner','inside','outside') for tt in (t.get(side) or []) if isinstance(tt, dict)}
            if tname in names:
                # take any input-side terminal with a wire that is not the one we came from
                for side in ('outer','inner','inside','outside'):
                    for tt in (t.get(side) or []):
                        if isinstance(tt, dict) and tt.get('name') != tname and (tt.get('wire') or 'const' in tt):
                            return resolve(tt, depth+1)
        return f"<tunnel {op}>"
    if op == 'Add':
        ins = [t for t in n['terminals'] if t['dir']=='in']
        return '(' + ' + '.join(resolve(t, depth+1) for t in ins) + ')'
    if op == 'Increment':
        ins = [t for t in n['terminals'] if t['dir']=='in']
        return '(' + resolve(ins[0], depth+1) + '+1)'
    if op == 'Feedback Node':
        return '<feedback>'
    return f"<{op or cls}#{nid}>"

# ---- tasks: which Create Task feeds which Create Channel chain; which Read/Write consume it
cvc = [n for n in nodes.values() if n.get('op') == 'DAQmx Create Virtual Channel.vi']
rows = []
for n in cvc:
    tin = {t['i']: t for t in n['terminals']}
    variant = n.get('poly_variant_name')
    phys = resolve(tin[5]) if 5 in tin else '?'
    name = resolve(tin[7]) if 7 in tin else '?'
    extra = ''
    if variant == 'AI Voltage':
        extra = f"max={resolve(tin[1])} min={resolve(tin[2])} termcfg={resolve(tin[3])}"
    else:
        # DI/DO: i=3 is line grouping? print remaining ins
        others = {i: resolve(t) for i, t in tin.items() if t['dir']=='in' and i not in (0,5,7,11,12)}
        extra = ' '.join(f"{i}={v}" for i, v in others.items())
    rows.append((variant, name, phys, extra, n['id'], parent_of.get(n['id'])))
rows.sort(key=lambda r: (r[0], r[1]))
for r in rows:
    print(f"{r[0]:15s} {r[1]:32s} {r[2]:45s} {r[3]}   [node {r[4]} in {r[5]}]")
