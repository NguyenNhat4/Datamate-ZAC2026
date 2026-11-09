"""Preparation CLI dispatch, intentionally independent of questions/answers."""
from datetime import datetime
import json
from pathlib import Path
import re

from .contracts import PreparationConfig, PreparationSpec
from .pipeline import prepare_business, profile_business


def run_preparation(args):
    split=Path(args.split).resolve()
    root=split/'data'
    if not root.is_dir():raise ValueError('Split needs a data directory')
    names=args.business.split(',') if args.business else sorted(p.name for p in root.iterdir() if p.is_dir())
    if not names or any(not re.fullmatch(r'[A-Za-z0-9_-]+',n) for n in names):
        raise ValueError('Invalid business selection')
    if args.spec and len(names)!=1:raise ValueError('A saved specification applies to one business at a time')
    spec=PreparationSpec.model_validate_json(Path(args.spec).read_text()) if args.spec else None
    output=Path(args.output or ('artifacts/profiles/'+datetime.now().strftime('%Y%m%d-%H%M%S-%f'))).absolute()
    if output.resolve().is_relative_to(split) or split.is_relative_to(output.resolve()):
        raise ValueError('Preparation reports must be outside the source split')
    if output.is_symlink() or any(p.is_symlink() for p in output.parents):raise ValueError('Output may not contain symlinks')
    adapter=None
    if args.model:
        from .assistance import LocalProposalAdapter
        adapter=LocalProposalAdapter(args.model,args.base_url)
    config=PreparationConfig(memory_limit=args.memory_limit,deadline_seconds=args.deadline,
                             model_configuration=adapter.configuration if adapter else {})
    results=[];failed=False
    try:
        for name in names:
            try:
                if args.command=='prepare':
                    result=prepare_business(root/name,split/'rulebook.md',split/'calendar.csv',
                                            output=output,config=config,spec=spec,proposal_adapter=adapter)
                    summary=result.model_dump(mode='json')
                else:
                    result=profile_business(root/name,split/'rulebook.md',split/'calendar.csv',
                                            config=config,spec=spec,proposal_adapter=adapter)
                    output.mkdir(parents=True,exist_ok=True)
                    report=output/(name+'-'+result.fingerprint+'.json')
                    if not report.exists():report.write_text(result.model_dump_json(indent=2)+'\n',encoding='utf-8')
                    summary={'business':name,'status':result.status,'profile':str(report),
                             'measurements':result.measurements,
                             'issues':{code:sum(i.count for i in result.issues if i.code==code) for code in sorted({i.code for i in result.issues})}}
                results.append(summary)
                failed |= result.status=='blocked'
                print(json.dumps({'business':name,**summary},ensure_ascii=False),flush=True)
            except (ValueError,OSError,RuntimeError,TimeoutError) as error:
                failed=True
                summary={'business':name,'status':'blocked','error':f'{type(error).__name__}: {error}'}
                results.append(summary);print(json.dumps(summary,ensure_ascii=False),flush=True)
        output.mkdir(parents=True,exist_ok=True)
        # Invocation summaries are distinct from immutable business bundles.
        summary_path=output/('run-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'.json')
        summary_path.write_text(json.dumps({'command':args.command,'businesses':results},ensure_ascii=False,indent=2,default=str)+'\n')
        return 1 if failed else 0
    finally:
        if adapter:adapter.close()
