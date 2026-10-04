"""
多进程分段筛（只筛奇数），支持断点续跑、上界终止、限速。

用法:
    python prime.py <max_n> [--workers N] [--rate R] [--batch-sleep S] [--max-file-mb M]

示例:
    python prime.py 1000000000
    python prime.py 1000000000000 --workers 4
    python prime.py 1000000000 --rate 50000          # 每秒最多 5 万个素数
    python prime.py 1000000000 --batch-sleep 1       # 每批跑完歇 1 秒
    python prime.py 1000000000 --max-file-mb 200     # 文件超 200MB 停
"""

import math
import os
import sys
import json
import time
import argparse
from math import isqrt
from multiprocessing import Pool, cpu_count

FILENAME = 'prime.txt'
STATE_FILE = 'prime_state.json'

TARGET_PRIMES_PER_SEGMENT = 50_000
MIN_SEGMENT = 1 << 16
MAX_SEGMENT = 1 << 24
TASKS_PER_WORKER = 4


# ---------------- 文件工具 ----------------

def ensure_trailing_newline_binary(path):
    if not os.path.exists(path):
        return
    with open(path, 'rb+') as f:
        f.seek(0, 2)
        if f.tell() == 0:
            return
        f.seek(-1, 2)
        if f.read(1) != b'\n':
            f.write(b'\n')


def load_last_prime(path):
    if not os.path.exists(path):
        return None
    last = None
    with open(path, 'r') as f:
        for line in f:
            for tok in line.split():
                try:
                    last = int(tok)
                except ValueError:
                    pass
    return last


def append_primes(f, primes):
    if not primes:
        return
    f.write(''.join(f"{p}\n" for p in primes))
    f.flush()
    os.fsync(f.fileno())


# ---------------- 状态 ----------------

def load_state():
    try:
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_state(state):
    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(state, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE_FILE)


# ---------------- 筛法 ----------------

def simple_sieve(limit):
    """普通筛，生成 [2, limit] 内所有素数。"""
    if limit < 2:
        return []
    sieve = bytearray([1]) * (limit + 1)
    sieve[0] = sieve[1] = 0
    for i in range(2, isqrt(limit) + 1):
        if sieve[i]:
            start = i * i
            sieve[start:limit + 1:i] = b'\x00' * (((limit - start) // i) + 1)
    return [i for i, v in enumerate(sieve) if v]


def adaptive_segment_size(cur):
    """段长按数值量级自适应（单位：奇数个数）。"""
    ln = math.log(cur) if cur > 2 else 1.0
    size = int(TARGET_PRIMES_PER_SEGMENT * ln)
    size = max(MIN_SEGMENT, min(MAX_SEGMENT, size))
    return size & ~1


# ---------------- 工作进程 ----------------

# 只筛奇数：段内索引 i 对应奇数 odd_start + 2*i
# base_primes 里去掉 2，只用奇素数标记
_BASE_ODD_PRIMES = None

def _init_worker(base_odd_primes):
    global _BASE_ODD_PRIMES
    _BASE_ODD_PRIMES = base_odd_primes


def sieve_odd_segment(args):
    """
    筛区间 [odd_start, odd_end]（闭区间，均为奇数）。
    返回该区间内的素数列表。
    """
    odd_start, odd_end = args
    if odd_end < odd_start or odd_start < 3:
        return []

    # 段内奇数个数
    count = (odd_end - odd_start) // 2 + 1
    sieve = bytearray([1]) * count
    base = _BASE_ODD_PRIMES

    for p in base:
        if p * p > odd_end:
            break
        # 找 >= max(p*p, odd_start) 的最小 p 的奇数倍数
        # p 的倍数为 p*(2k+1)，我们要 p*m 是奇数 => m 奇
        # 先找 >= odd_start 的最小 p 的倍数
        m = (odd_start + p - 1) // p
        if m < p:
            m = p
        if m % 2 == 0:
            m += 1  # 保证倍数 m 为奇，乘积才是奇
        first = p * m
        if first > odd_end:
            continue
        # first 对应索引
        offset = (first - odd_start) // 2
        # 步长：p 的奇数倍数之间差 2p => 索引差 p
        sieve[offset::p] = b'\x00' * (((count - 1 - offset) // p) + 1)

    return [odd_start + 2 * i for i, v in enumerate(sieve) if v]


# ---------------- 限速器 ----------------

class RateLimiter:
    """简单的令牌桶：保证平均产出不超过 rate 个/秒。"""
    def __init__(self, rate_per_sec):
        self.rate = rate_per_sec
        self.allowance = float(rate_per_sec)
        self.last = time.monotonic()

    def consume(self, n):
        if self.rate is None or self.rate <= 0:
            return
        now = time.monotonic()
        elapsed = now - self.last
        self.last = now
        self.allowance += elapsed * self.rate
        # 桶容量限制为一个 rate，避免长时间不跑后突发
        if self.allowance > self.rate:
            self.allowance = float(self.rate)
        self.allowance -= n
        if self.allowance < 0:
            sleep_time = -self.allowance / self.rate
            time.sleep(sleep_time)
            self.allowance = 0.0


# ---------------- 主流程 ----------------

def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument('max_n', type=int, help='上界，跑到此为止')
    ap.add_argument('--workers', type=int, default=None,
                    help='工作进程数，默认 cpu_count()-1')
    ap.add_argument('--rate', type=float, default=None,
                    help='每秒最多产出多少素数（不限则省略）')
    ap.add_argument('--batch-sleep', type=float, default=0.0,
                    help='每批跑完强制 sleep 秒数')
    ap.add_argument('--max-file-mb', type=float, default=None,
                    help='prime.txt 超过此大小 (MB) 就停止')
    return ap.parse_args()


def main():
    args = parse_args()
    max_n = args.max_n
    if max_n < 2:
        print("max_n too small")
        return

    base_limit = isqrt(max_n) + 1
    print(f"target max_n = {max_n}, base_limit = {base_limit}")
    base_primes = simple_sieve(base_limit)
    base_odd_primes = [p for p in base_primes if p != 2]
    print(f"base primes: {len(base_primes)} (odd: {len(base_odd_primes)})")

    # 断点
    ensure_trailing_newline_binary(FILENAME)
    file_last = load_last_prime(FILENAME)
    state = load_state()

    if state is None:
        if file_last is None:
            with open(FILENAME, 'a') as f:
                append_primes(f, [2])
            next_odd = 3
            last_written = 2
            print("[fresh start] wrote 2")
        else:
            # 文件最后可能是偶数？正常不会。取 max(3, file_last+1) 并对齐奇数
            nxt = max(3, file_last + 1)
            if nxt % 2 == 0:
                nxt += 1
            next_odd = nxt
            last_written = file_last
            print(f"[no state] resume from file tail: {file_last}")
    else:
        next_odd = state['next_odd']
        last_written = state.get('last_written', file_last)
        print(f"[resume] next_odd={next_odd}, last_written={last_written}")
        if (file_last is not None and last_written is not None
                and file_last != last_written):
            print(f"[warn] file_last={file_last} != state last={last_written}; "
                  f"falling back to file tail")
            nxt = max(3, file_last + 1)
            if nxt % 2 == 0:
                nxt += 1
            next_odd = nxt
            last_written = file_last

    if next_odd < 3:
        next_odd = 3
    if next_odd % 2 == 0:
        next_odd += 1

    num_workers = args.workers if args.workers else max(1, cpu_count() - 1)
    print(f"using {num_workers} worker processes; start odd = {next_odd}")

    limiter = RateLimiter(args.rate)
    max_file_bytes = (int(args.max_file_mb * 1024 * 1024)
                      if args.max_file_mb else None)

    f = open(FILENAME, 'a')
    pool = Pool(num_workers, initializer=_init_worker,
                initargs=(base_odd_primes,))
    total_new = 0
    cur_odd = next_odd

    try:
        while cur_odd <= max_n:
            batch = num_workers * TASKS_PER_WORKER
            tasks = []
            for _ in range(batch):
                if cur_odd > max_n:
                    break
                # 段长以"奇数个数"计
                seg_count = adaptive_segment_size(cur_odd)
                odd_start = cur_odd
                odd_end = odd_start + 2 * (seg_count - 1)
                if odd_end > max_n:
                    # 对齐到不超过 max_n 的最大奇数
                    m = max_n if max_n % 2 == 1 else max_n - 1
                    if m < odd_start:
                        break
                    odd_end = m
                tasks.append((odd_start, odd_end))
                cur_odd = odd_end + 2

            if not tasks:
                break

            results = pool.map(sieve_odd_segment, tasks)
            all_new = []
            for seg in results:
                all_new.extend(seg)

            if all_new:
                append_primes(f, all_new)
                last_written = all_new[-1]
                total_new += len(all_new)
                limiter.consume(len(all_new))

            save_state({
                'next_odd': cur_odd,
                'last_written': last_written,
                'max_n': max_n,
            })

            print(f"batch: +{len(all_new)} primes, "
                  f"total_new={total_new}, last={last_written}, "
                  f"next_odd={cur_odd}")

            # 文件大小限制
            if max_file_bytes is not None:
                size = os.path.getsize(FILENAME)
                if size >= max_file_bytes:
                    print(f"[stop] file size {size/1024/1024:.1f} MB "
                          f">= limit {args.max_file_mb} MB")
                    break

            # 批次间强制 sleep
            if args.batch_sleep > 0:
                time.sleep(args.batch_sleep)

    except KeyboardInterrupt:
        print("\n[interrupted]")
    finally:
        if last_written is not None:
            save_state({
                'next_odd': cur_odd,
                'last_written': last_written,
                'max_n': max_n,
            })
        pool.terminate()
        pool.join()
        f.close()
        print(f"done. total_new={total_new}, last_written={last_written}")


if __name__ == '__main__':
    main()
