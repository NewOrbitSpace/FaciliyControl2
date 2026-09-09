import json, binascii, collections, sys
exec(open('trace.py').read().split("# ---- tasks")[0])
refmap = {int(k): v for k, v in json.load(open('refmap.json')).items()}
STATE = ['Facility Off','Pumping to Rough','Overnight Pump','Engage Turbo','Pumping to High Vac','Disengage Turbo','Vent and Shutdown','Turbo slowing','Venting']

def refname(n):
    labs = refmap.get(n.get('param_idx'), [])
    return labs[0] if labs else f"REF[{n.get('param_idx')}]"

# tunnel index: inner wire -> (structure id, outer wire); outer wire -> structure id
INNER, OUTER = {}, {}
for _n in nodes.values():
    for _t in _n.get('tunnels', []) or []:
        if _t.get('inner_wire'): INNER[_t['inner_wire']] = (_n['id'], _t.get('outer_wire'), _t.get('type'), _t.get('mode'))
        if _t.get('outer_wire'): OUTER[_t['outer_wire']] = (_n['id'], _t.get('inner_wire'), _t.get('type'))
def val(term, depth=0):
    """resolve an input terminal to a readable expression"""
    if depth > 14: return '…'
    wid0 = term.get('wire')
    if wid0 and wid0 in INNER and 'const' not in term:
        sid, outer, ttype, mode = INNER[wid0]
        if ttype in ('lSR','rSR'): return f"SR[{sid}]"
        if outer: return val({'wire': outer}, depth+1)
        return f"<tunnel {ttype} of #{sid}>"
    if 'const' in term:
        v = term['const']
        ty = term.get('type','')
        if ty == 'String': return repr(dec(v))
        if ty == 'Boolean': return 'T' if v.endswith('1') else 'F'
        if ty.startswith('NumFloat64') and len(v) == 16:
            import struct; return repr(struct.unpack('>d', binascii.unhexlify(v))[0])
        if ty.startswith('NumInt') or ty.startswith('NumUInt') or ty.startswith('enum'):
            try: return str(int(v, 16))
            except: return v
        return f"<{ty}:{v}>"
    wid = term.get('wire')
    if not wid: return '_'
    src = source_of(wid)
    if not src: return f'<w{wid}>'
    nid, tname = src
    n = nodes.get(nid)
    if n is None: return f'<n{nid}>'
    cls, op = n.get('class'), n.get('op','')
    if cls == 'Constant':
        v = n.get('value',''); ty = n.get('type','')
        if len(v) == 16 and ('Float' in ty or ty.startswith('TypeID')):
            try:
                import struct; return repr(struct.unpack('>d', binascii.unhexlify(v))[0])
            except: pass
        if len(v) <= 8:
            try: return str(int(v,16))
            except: return v
        return repr(dec(v))
    if cls == 'gRef': return refname(n)
    if cls in ('case','select','flat_sequence','loop','forLoop','whileLoop'):
        for t in n.get('tunnels', []) or []:
            if t.get('inner_wire') == wid:  # value entering the structure from outside
                return val({'wire': t.get('outer_wire')}, depth+1) if t.get('outer_wire') else '_'
            if t.get('outer_wire') == wid:  # value leaving the structure -> summarise
                return f"<out of {op}#{nid}>"
        return f"<{op}#{nid}>"
    ins = [t for t in n.get('terminals', []) if t['dir']=='in']
    outs = [t for t in n.get('terminals', []) if t['dir']=='out']
    args = lambda: ', '.join(val(t, depth+1) for t in ins)
    if op in ('Concatenate Strings',): return ' + '.join(val(t, depth+1) for t in ins)
    if op in ('And','Or','Add','Subtract','Multiply','Divide','Equal?','Not Equal?','Greater?','Less?','Greater Or Equal?','Less Or Equal?','Exclusive Or','Not Exclusive Or'):
        sym = {'And':'&&','Or':'||','Add':'+','Subtract':'-','Multiply':'*','Divide':'/','Equal?':'==','Not Equal?':'!=','Greater?':'>','Less?':'<','Greater Or Equal?':'>=','Less Or Equal?':'<=','Exclusive Or':'XOR','Not Exclusive Or':'XNOR'}[op]
        return '(' + f' {sym} '.join(val(t, depth+1) for t in ins) + ')'
    if op == 'Not': return f"!{val(ins[0], depth+1)}"
    if op == 'Select':
        byname = {t['name']: t for t in ins}
        if {'selector','t_value','f_value'} <= set(byname):
            return f"({val(byname['selector'],depth+1)} ? {val(byname['t_value'],depth+1)} : {val(byname['f_value'],depth+1)})"
        return f"Select({args()})"
    if op == 'Compound Arithmetic': return f"CompoundArith[{n.get('operation','?')}]({args()})"
    if op == 'Index Array':
        k = [t['name'] for t in outs].index(tname) if tname in [t['name'] for t in outs] else 0
        return f"{val(ins[0],depth+1)}[{val(ins[1+k],depth+1) if 1+k < len(ins) else '?'}]"
    if op == 'Build Array': return '[' + args() + ']'
    if op == 'Bundle/Unbundle By Name':
        return f"{op}({args()})"
    if op == 'Feedback Node': return f"prev({args()})"
    if op == 'Boolean To (0,1)': return f"b01({args()})"
    if op.startswith('Elapsed Time'):
        outs_n = [t['name'] for t in outs]
        k = outs_n.index(tname) if tname in outs_n else -1
        which = {0:'elapsed_s',1:'elapsed_str',2:'present_s',3:'present_str',4:'start_s',5:'DONE',6:'start_str'}.get(k, f'out{k}')
        return f"{op}.{which}(target={val(ins[0],depth+1)}, reset={val(ins[1],depth+1)}, autoreset={val(ins[2],depth+1)})"
    if op in ('Increment','Decrement'): return f"{op}({args()})"
    if 'Control Reference' in op: return refname(n)
    if cls in ('fPTerm','term') or op in ('Chiller_User_Cmd','Primary_User_Cmd') or (op and op.replace(' ','') and not op.endswith('.vi') and len(ins)==0):
        return f"«{op}»"   # a front-panel control terminal
    return f"{op}#{nid}({args()})" if ins else f"{op}#{nid}"

def sinks_of(wid):
    """where does a wire go (writes to locals / indicators / tunnels)"""
    out = []
    for w in d['wires']:
        if w['id'] == wid:
            tn = nodes.get(w['to']['node'])
            if tn is None: out.append(f"→n{w['to']['node']}"); continue
            if tn.get('class') == 'gRef': out.append(f"→WRITE {refname(tn)}")
            elif tn.get('class') in ('case','select','flat_sequence','loop','forLoop','whileLoop'): out.append(f"→{tn.get('op')}#{tn['id']}")
            else: out.append(f"→{tn.get('op')}#{tn['id']}")
    return out

def explain(struct_id, frame_idx=None, indent=''):
    n = nodes[struct_id]
    frames = n.get('frames') or [{'frame': None, 'body': n.get('body')}]
    for fi, f in enumerate(frames):
        if frame_idx is not None and str(f.get('frame')) != str(frame_idx): continue
        print(f"{indent}▌ {n.get('op')}#{struct_id} frame {f.get('frame')}  selector={val(n.get('selector') or {})}")
        body = f.get('body') or []
        for m in body:
            if not isinstance(m, dict): continue
            op = m.get('op',''); cls = m.get('class')
            if cls in ('case','select'):
                print(f"{indent}  ┌ Case#{m['id']} on {val(m.get('selector') or {})}  frames={[x.get('frame') for x in m.get('frames',[])]}")
                explain(m['id'], None, indent+'  │ ')
                continue
            if cls in ('flat_sequence','loop','forLoop','whileLoop'):
                print(f"{indent}  ┌ {op}#{m['id']}")
                explain(m['id'], None, indent+'  │ ')
                continue
            ins = [t for t in m.get('terminals',[]) if t['dir']=='in']
            outs = [t for t in m.get('terminals',[]) if t['dir']=='out']
            if cls == 'gRef':
                t = m['terminals'][0]
                if t['dir']=='in': print(f"{indent}  WRITE {refname(m)} <- {val(t)}")
                continue
            if cls == 'Constant' or not (ins or outs): continue
            sinks = sum((sinks_of(t['wire']) for t in outs if t.get('wire')), [])
            argtxt = ', '.join(val(t) for t in ins)
            if op in ('Bundle/Unbundle By Name','Merge Errors','Clear Errors.vi','Simple Error Handler.vi'): continue
            print(f"{indent}  {op}#{m['id']}({argtxt})  {' '.join(sinks)}")
if __name__ == '__main__':
    sid = sys.argv[1]; fr = sys.argv[2] if len(sys.argv) > 2 else None
    explain(sid, fr)
