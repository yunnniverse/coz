import time
import sys

def counter_until(limit: int):
    start = time.time()
    count = 0

    while count < limit:
        count += 1
        # 1% 단위로 진행 상황 표시
        if count % (limit // 100) == 0:
            elapsed = time.time() - start
            percent = (count / limit) * 100
            sys.stdout.write(
                f"\rProgress: {percent:.2f}% | Elapsed: {elapsed:.6f} s"
            )
            sys.stdout.flush()

    end = time.time()
    elapsed = end - start
    print(f"\n✅ Done! Counter reached {limit} in {elapsed:.6f} s.")

if __name__ == "__main__":
    counter_until(50_000_000)  # 예: 5천만까지 카운트
