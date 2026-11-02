"""Reproducible evaluation partitions; never used in answer-generation logic."""
import hashlib
import random
from collections import defaultdict


def business_partition(business_id, seed=42):
    digest = hashlib.sha256(f'{seed}:{business_id}'.encode()).digest()
    bucket = int.from_bytes(digest[:8], 'big') % 10
    return 'dev' if bucket < 6 else ('validation' if bucket < 8 else 'holdout')


def select_questions(questions, *, partition='all', partition_seed=42, businesses=None,
                     limit=0, seed=42, sample='random'):
    if partition not in {'all', 'dev', 'validation', 'holdout'}:
        raise ValueError('Unknown evaluation partition')
    if sample not in {'random', 'balanced'}:
        raise ValueError('Unknown sample method')
    if limit < 0:
        raise ValueError('limit cannot be negative')
    selected = [q for q in questions
                if (partition == 'all' or business_partition(q['business_id'], partition_seed) == partition)
                and (not businesses or q['business_id'] in businesses)]
    if not limit or limit >= len(selected):
        return selected
    rng = random.Random(seed)
    if sample == 'random':
        return rng.sample(selected, limit)
    groups = defaultdict(list)
    for q in selected:
        groups[q['business_id']].append(q)
    names = sorted(groups)
    rng.shuffle(names)
    for name in names:
        rng.shuffle(groups[name])
    result = []
    while len(result) < limit:
        for name in names:
            if groups[name] and len(result) < limit:
                result.append(groups[name].pop())
    return result
