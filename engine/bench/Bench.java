// cogcraft 客户端性能微基准 —— 用与游戏相同的 JRE 运行，输出一行 JSON。
// 统计：每项 3 轮各 150ms，取最好那轮（噪声只让结果变慢，最好值比均值稳）。
// 可比性：固定堆 512MB + G1；每项测固定「时长」而非固定「次数」。
// 真实性：分配测试保留 256MB 常驻数据，让 G1 有真实搬运工作，并记录最大单次停顿。
import java.lang.management.*;
import java.util.*;

public class Bench {
    static final long ROUND = 150_000_000L;
    static final int  N     = 3;
    static final long WARM  = 300_000_000L;
    static long now(){ return System.nanoTime(); }

    static long intWork(long dur){
        long end = now() + dur, ops = 0;
        long x = 0x9E3779B97F4A7C15L;
        while (true) {
            for (int i = 0; i < 4096; i++) {
                x ^= x << 13; x ^= x >>> 7; x ^= x << 17;
                if ((x & 0xFFFF) < 0x4000) ops++; else ops += 2;
            }
            ops += 4096;
            if (now() >= end) break;
        }
        return ops;
    }

    static int[] RING;
    static void buildRing(){
        final int SZ = 1 << 22;                  // 16MB 环，跨出 L2/L3 → 测 DRAM 延迟
        RING = new int[SZ];
        Random r = new Random(12345);
        int[] perm = new int[SZ];
        for (int i = 0; i < SZ; i++) perm[i] = i;
        for (int i = SZ - 1; i > 0; i--) { int j = r.nextInt(i + 1);
            int t = perm[i]; perm[i] = perm[j]; perm[j] = t; }
        for (int i = 0; i < SZ; i++) RING[perm[i]] = perm[(i + 1) % SZ];
    }
    static double chaseOnce(long dur){
        int[] ring = RING;
        long end = now() + dur, steps = 0;
        int p = 0;
        while (true) {
            for (int i = 0; i < 8192; i++) p = ring[p];
            steps += 8192;
            if (now() >= end) break;
        }
        return (double) dur / steps;             // ns / 跳
    }

    // 常驻 256MB，让 G1 每次 young GC 都要真搬东西（MC 的实况：常驻几 GB）
    static long[][] live = new long[8][];
    static void buildLive(){ for (int i = 0; i < live.length; i++) live[i] = new long[1 << 22]; }

    static long GC_TOTAL_CNT = -1;
    static double GC_PCT = -1, STALL_MS = -1;
    static double allocOnce(long dur){
        long gc0 = 0, cn0 = 0;
        for (GarbageCollectorMXBean b : ManagementFactory.getGarbageCollectorMXBeans()) {
            if (b.getCollectionTime() > 0) gc0 += b.getCollectionTime();
            if (b.getCollectionCount() > 0) cn0 += b.getCollectionCount();
        }
        long end = now() + dur, n = 0, worst = 0; double sink = 0;
        while (true) {
            long b0 = now();
            for (int i = 0; i < buf.length; i++) {
                Vec3 v = new Vec3(i, i + 1, i + 2);
                buf[i] = v; sink += v.x;
            }
            long d = now() - b0;
            if (d > worst) worst = d;
            n += buf.length;
            if (now() >= end) break;
        }
        long gcSum = 0, cnSum = 0;
        for (GarbageCollectorMXBean b : ManagementFactory.getGarbageCollectorMXBeans()) {
            if (b.getCollectionTime() > 0) gcSum += b.getCollectionTime();
            if (b.getCollectionCount() > 0) cnSum += b.getCollectionCount();
        }
        GC_PCT     = (gcSum - gc0) * 1e6 / dur * 100.0;
        GC_TOTAL_CNT = cnSum - cn0;
        STALL_MS   = worst / 1e6;                // 单批（2500 个对象）最长耗时 ≈ 玩家感知的「卡一下」
        return n / (dur / 1e9) / 1e6;            // 百万个对象/秒
    }
    static Object[] buf = new Object[2500];
    static final class Vec3 { final double x, y, z;
        Vec3(double x, double y, double z){ this.x=x; this.y=y; this.z=z; } }

    static double copyOnce(long dur){
        int[] src = new int[1 << 20], dst = new int[1 << 20];
        long end = now() + dur, bytes = 0;
        while (true) {
            System.arraycopy(src, 0, dst, 0, src.length);
            bytes += (long) src.length * 4;
            if (now() >= end) break;
        }
        return bytes / (dur / 1e9) / 1e9;        // GB/s
    }

    public static void main(String[] a){
        buildRing(); buildLive();
        intWork(WARM); chaseOnce(WARM); allocOnce(WARM); copyOnce(WARM);

        long t1 = now();
        double intBest = 0, chaseBest = 1e9, allocBest = 0, copyBest = 0;
        double gcBest = -1, stallWorst = 0; long cntSum = 0;
        for (int i = 0; i < N; i++) {
            intBest   = Math.max(intBest,  intWork(ROUND) / (ROUND / 1e9) / 1e6);
            chaseBest = Math.min(chaseBest, chaseOnce(ROUND));
            allocBest = Math.max(allocBest, allocOnce(ROUND));
            if (GC_PCT > gcBest) gcBest = GC_PCT;
            if (STALL_MS > stallWorst) stallWorst = STALL_MS;
            cntSum += GC_TOTAL_CNT;
            copyBest  = Math.max(copyBest,  copyOnce(ROUND));
        }
        Runtime rt = Runtime.getRuntime();
        System.out.println(String.format(Locale.ROOT,
            "{\"jvm\":\"%s\",\"cores\":%d,\"heap_mb\":%d," +
            "\"int_mops\":%.1f,\"chase_ns\":%.1f,\"alloc_mps\":%.1f," +
            "\"gc_pct\":%.2f,\"gc_cnt\":%d,\"stall_ms\":%.2f,\"copy_gbs\":%.1f," +
            "\"bench_ms\":%d}",
            System.getProperty("java.version"), rt.availableProcessors(),
            rt.maxMemory() / 1048576,
            intBest, chaseBest, allocBest, gcBest, cntSum, stallWorst, copyBest,
            (now() - t1) / 1_000_000));
    }
}
