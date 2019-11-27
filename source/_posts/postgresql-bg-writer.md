---
title: Postgresql BgWriter 原理
date: 2019-11-27 21:18:08
tags: postgresql, bg-writer
categories: postgresql
---



## 前言

postgresql 有一个后台进程 bgwriter，它会定时刷新缓存到文件系统中。这种机制提高了缓存的替换速度，因为在寻找空闲缓存时，有时需要将脏页刷新到文件中，而刷新操作是比较耗时的。同样它也提高了执行 checkpoint 的完成速度，因为 checkpoint 需要刷新所有的脏页。



## 监控

BgWriter 的监控只能从 **`pg_stat_bgwriter`**视图查看，它只是记录了统计数据

| Column             | Type     | Description                                    |
| :----------------- | :------- | :--------------------------------------------- |
| `buffers_clean`    | `bigint` | bgwriter 刷新缓存的总数                        |
| `maxwritten_clean` | `bigint` | 因为达到最大缓存刷新数目，而bgwriter退出的次数 |
| `buffers_alloc`    | `bigint` | 用户的可用缓存分配数目                         |



## BgWriter 进程

bgwriter 进程的原理很简单，它只是定期的执行缓存刷新。它有两个状态，正常状态和冬眠状态。在正常状态下，bgwriter 在刷新完缓存后，会等待时长`bgwriter_delay`（可以在`postgresql.conf`指定，默认值为200ms）。当连续两次都没有要刷新的缓存，那么就会进入冬眠状态，这时的等待时长变为`50 * bgwriter_delay`。



## 执行原理

BgWriter 进程会维护了一些统计数据，这些数据会影响到刷新缓存的执行。在介绍下面内容之前，读者需要了解下 clock sweep 缓存替换算法，可以参考此篇文章。



### 确认遍历数目和位置

postgresql 的缓存是由一个环形数组来表示，clock sweep 算法会记录上次的遍历结束位置和已遍历的缓存轮数。我们把 clock sweep 算法的遍历位置记为`strategy_id`，缓存轮数记为`strategy_pass`。当然 bgwriter 也会记录上次的遍历结束位置和遍历轮数，分别记为`next_to_clean_id`和`next_to_clean_passes`。

首先确定遍历的缓存数目和起始位置。遍历的方向只能从前往后，并且需要此次 bgwriter 能够遍历到`strategy_id`。分为下面三种情况：

1.`next_to_clean_passes` > `strategy_pass` 并且 `strategy_id > next_to_clean_id`，那么遍历数目为`strategy_id - next_to_clean_id`，否则不需要遍历。

2.`next_to_clean_passes` == `strategy_pass` 并且 `next_to_clean_id > strategy_id`，那么遍历数目为`NBuffers - (next_to_clean - strategy_buf_id)`

3.其他情况，遍历数目为`NBuffers`，起始位置为`strategy_id`，更新`next_clean_id = strategy_id`



### 预估缓存替换数目

bgwriter 还会使用以往的缓存替换数据，来推断此次 bgwriter 的缓存需求数目和。



计算此次 bgwriter 的缓存需求数目，采用了平滑算法，

1. `recent_alloc`表示距离上次 bgwrite 期间的缓存需求数
2. `smoothing_samples`参数为16，表示平滑程度。
3. `smoothed_alloc`表示平滑之后的平均值，初始值为0。

```c
if (smoothed_alloc <= (float) recent_alloc)
    // 为了保证此次缓存替换数目大，没有使用平滑算法
    smoothed_alloc = recent_alloc;
else
    // 计算平滑之后的值
    smoothed_alloc += ((float) recent_alloc - smoothed_alloc) / smoothing_samples;
```

其实 `smoothed`已经可以作为此次 bgwriter 到下次期间的缓存需求预估值，但是为了应对请求突然增大的情况，这里会再乘以`bgwriter_lru_multiplier`因子（可以在`postgresql.conf`配置）

```c
// 计算预估值
upcoming_alloc_est = (int) (smoothed_alloc * bgwriter_lru_multiplier);
```



因为缓存遍历是从`next_clean_id`开始向后遍历，直到遇到`strategy_id`为止。而从`strategy_id`到`next_clean_id`的这段距离，是有其他进程遍历的，我们需要估算出这部分的可用缓存数量。

通过上次 bgwriter 到此次的统计数据来估算。这里先估算出来找到平均一个可用缓存需要遍历的缓存数目

```c
// clock sweep 遍历的缓存数目，缓存数组的长度为NBuffers
strategy_delta = strategy_buf_id - prev_strategy_buf_id;
strategy_delta += (long) passes_delta * NBuffers;

// 计算上次期间的平均遍历数目
scans_per_alloc = (float) strategy_delta / (float) recent_alloc;
// 平滑操作
smoothed_density += (scans_per_alloc - smoothed_density) / smoothing_samples;
```

`strategy_id`到`next_to_clean_id`这段距离，预估的可用缓存数目

```c
reusable_buffers_est = (float) (NBuffers - bufs_to_lap) / smoothed_density;
```





### 执行完成条件

bgwriter 会从第一步计算出的遍历起始位置，开始遍历。

1. 如果该缓存的引用次数或使用次数不为零，就会遍历下一个缓存。
2. 如果该缓存的引用次数或使用次数都为零，认为该缓存是可用缓存。
3. 如果该缓存为脏页，就刷新到文件并且发送局部sync请求后，也认为该缓存是可用缓存。



当它满足下面三个条件之一，就会认为此次 bgwrite 完成。

1.遍历的缓存数目已经达到结束位置（在第一步中确认遍历数目和位置）

2.遍历过程中，`reusable_buffers_est + 遍历过程中的可用缓存数目  >= upcoming_alloc_est`

3.刷新的数目超过了`bgwriter_lru_maxpages`（可以在`postgresql.conf`中配置）



### 局部 sync

注意上面的脏页刷新只是写入到了文件系统的cache里，并不代表着持久化到磁盘，所以它会发送局部 sync 请求。当请求量超过了`bgwriter_flush_after`，就会处理堆积的 sync 请求。注意到这里是局部 sync 请求，它在处理时调用了`sync_file_range`方法，只是文件中指定的一段内容持久化到磁盘，而不是整个文件。



## 配置参数

| 名称                      | 含义                                   | 默认值 |
| ------------------------- | -------------------------------------- | ------ |
| `bgwriter_delay`          | 每次执行bgwriter的间隔时间             | 200ms  |
| `bgwriter_lru_maxpages`   | 每次执行bgwriter，刷新缓存的最大数目   | 100    |
| `bgwriter_lru_multiplier` | 在预估需要缓存数目的因子               | 2      |
| `bgwriter_flush_after`    | 当局部sync请求达到的最大数，会触发处理 | 512KB  |

`bgwriter_flush_after`这个配置项比较特殊，如果没有附带单位，那么表示缓存的个数。如果有，表示缓存的容量。