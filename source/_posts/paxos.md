---
title: Paxos 算法学习
date: 2019-09-11 22:47:21
tags: paxos
categories: zookeeper
---



## 前言

最近因为要了解 zookeeper 的原理，所以开始了学习分布式共识算法 paxos 。在网上可以搜到很多篇这方面的内容，但是网上的资料良莠不齐，导致学习起来比较曲折。本篇博客主要是总结了自己的学习成果，也可以供读者参考。paxos 是指一系列的相关算法，都是基于 basic paxos 衍生出来。multi-paxos 是最常用的算法，用来负责日志复制。



## basic paxos

这里首推 Lamport 的论文 [Paxos Made Simple](https://lamport.azurewebsites.net/pubs/paxos-simple.pdf )，这篇论文通过首先设计了一个简单的场景，然后提出这个场景的问题，通过一步步的优化和约束，最后推导出了最终方案。接下来会直接介绍最终方案，里面的演化和推算可以自己查看论文。

我们首先来明确下 paxos 解决了什么问题，简而言之就是在分布式的环境中，存在多个值，需要从中选定出一个值，达成共识。进一步来讲，分布式意味着有多个参与者，并且参与者之间的通信是不稳定的，通信速度有时快有时慢，甚至还会丢失。现在有多个值，如何在这种环境下，让参与者都能达成共识，选定出一个值。

在 paxos 算法里，值被包含在议案里，并且每个议案还有一个唯一的编号。围绕着议案，参与者分为 proposer，acceptor，learner 三种。proposer 负责提出议案，acceptor 负责接受议案，learner 负责选定最终议案。

paxos 分为三个阶段运行，Prepare 阶段、Accept 阶段、Learn 阶段。网上通常认为只有前两个阶段，这里额外加了一个 Learn 阶段，强调了 learner 的重要性。



### Prepare 阶段

proposer 在提出议案 N 之前，N 是该议案的编号，需要向至少多数的 acceptor 发送 Prepare（N）请求。

acceptor 收到 Prepare（N）请求后，

1. 如果之前已经接受了其他议案，那么就会返回成功响应该议案。
2. 如果之前收到过Prepare（M）请求，并且 M > N，那么可以忽略此次请求，也可以返回一个错误。
3. 如果之前收到过Prepare（M）请求，并且 M < N，那么 acceptor 会保证以后不会响应议案编号小于 N 的任何请求，并且返回成功响应。

```python
# Proposer
for acceptor in acceptors:
  send_prepare_req(num)

# Acceptor
if has_accepted:
    reply({ "num": highest_accepted_proposal.num, "value":  highest_accepted_proposal.value})
elif prepare_req.num < highest_prepare_proposal_num::
    reply("error")
else:
    reply("ok")
else:
  highest_prepare_proposal_num = prepare_req.num
  reply({ "num": highest_accepted_proposal.num, "value":  highest_accepted_proposal.value})
```



### Accept 阶段

proposer 在收到多数 acceptor 的成功响应后，如果这些acceptor中有返回议案，那么就从中选出那个编号最大的议案的值，作为接下来要提交的。如果没有返回议案，那么将本身的议案作为提交议案。

在确定好提交议案后，proposer 会向至少多数的 acceptor 发起 ACCEPT 请求，提交议案。注意下这里的多数acceptor可以不和prepare 阶段的相同。

acceptor 当收到 ACCEPT 请求后，会检查议案编号。因为acceptor 在 prepare 阶段，已经承诺过不会议案编号小于 N 的请求，如果 ACCEPT 请求的议案编号小于 N，那么就会拒绝接受此议案。否则，就会接受该议案。

当 proposer 收到多数 acceptor 的成功接收议案的响应后，就会认为该议案被选定了。

```python
# Proposer
value = max_num_proposal(prepare_responses).value
if value is None:
    value = my_proposal.value
for acceptor in acceptors:
  send_accept_req(my_proposal.num, value)

# Acceptor
if accept_req.num >= highest_prepare_proposal_num:
  highest_accepted_proposal = {"num": accept_req.num, "value": accept_req.value}
  reply("ok")
else:
  reply("error")
```



### Learn 阶段

每当 acceptor 接受了一个议案，就会立即通知 learner。learner 会记录每个 acceptor 接收的议案，如果一个议案被多数 acceptor 接受了，那么就决定选定该议案。注意到 leaner 选定了议案，才是整个 paxos 算法的结束。



## 编号生成

上面说到每个议案的编号都是唯一的，但是存在着多个 proposer，如何能保证它们提出的议案编号都不相同呢。我们可以使用第三方服务来生成递增编号，但是这样会依赖外部服务。我们可以指定每个 proposer 生成编号的规则，这样就能不依赖外部服务。最简单的可以用以下两种规则：

质数算法：每个 proposer 对应着唯一的质数，每个新增议案，就是该质数的倍数。比如有两个 proposer 对应着 2，3 两个质数，第一个 proposer 提出第 i 个议案时，它的编号时 2 * i 。

等差算法：假设有 n 个 proposer，那么第 m 个 proposer第 i 次新增的议案编号是 m + i * n 。



## 常见示例

这里会列举一下一些示例，来加深下 paxos 的理解。假设我们有 A，B，C 三个 acceptor，并且有两个 proposer，和两个议案。

<img src="paxos-example-1.svg">

learner 在 t4 时刻就感知到议案 N+1 已经被多数 acceptor 接受了，所以会选定议案 N+1。其实运行到这里，paxos 算法已经结束了。

但是 proposer 1 不能保证 proposer 2 能够正常完成提交议案，因为 proposer 2 有可能中途会挂掉，所以 proposer 1 只能继续提交议案，直到确认有一个议案被选定。我们之前说过每个议案的编号是唯一的，proposer 1 会提起一个新的议案，编号要比之前的大，这里为 N+2。

<img src="paxos-example-2.svg">

注意到 accept A 在接受了议案 N 后，还是可以改变接受议案 N+1。这一点在容易被忽略，甚至有些网上的博客还认为 acceptor 不能改变议案，这是错误的认识。



## multi-paxos

上面 basic paxos 算法是用来确认一个值的，但是仅仅是确认一个值，对于我们来说，使用场景太少。如果能确认多个值，那么就更好。既然运行一次 basic paxos 算法能确认一个值，那么为每个值都单独运行一次，就可以确认多个值了。我们称运行一次 basic paxos 算法，称作为一个实例。这个思想在 Paxos Made Simple 论文的最后一部分有提到过，它被用来实现一个状态机。

运行一个 basic paxos 实例，proposer 至少需要和 acceptor 通信两次（prepare 请求和 accept 请求），如果能在大部分时间里，选定一个 proposer（被称为 leader），只有它才能提出方案，这样就能将两次通信转化为一次（accept 请求）。算法流程如下：

1. 当一个 proposer 被选定为 leader 后，它会首先向多数的 acceptor 发起 prepare（N）请求。
2. acceptor 在接收到这个请求后，如果之前没有接受过其余的 prepare 请求，或者接受过的prepare（M）请求并且 M < N，那么就保证以后不会接受编号小于 N 的 accept 请求。否则忽略请求或者返回错误。
3. proposer 在收到多数 acceptor 的保证后，就认为自己成为 leader。对于接下来的值，直接发送 accept（N，Vi）请求给多数 acceptor。在收到多数 acceptor 的成功响应后，这个值就被确定下来了。

注意到 leader 的存在仅仅是性能的优化，multi-paxos 算法是可以允许有多个 leader 同时存在的，而不会导致系统混乱。假如有多个 leader 存在，后者发起 prepare 的编号肯定比前者大，那么就会导致前者的 accept 请求会失败，最后后者会成为真正的 leader。



## 参考资料

下面的链接是我认为质量比较高的，读者可以阅读一下：

[1]  https://lamport.azurewebsites.net/pubs/paxos-simple.pdf ， Paxos Made Simple 

[2]  https://www.cnblogs.com/linbingdong/p/6253479.html， Paxos Made Simple 翻译，不过中间会有些错误的部分，在底下的评论里有说明

[3]  https://cloud.tencent.com/developer/article/1158799， 详细的介绍了 multi-paxos 算法

[4]  https://mp.weixin.qq.com/s?__biz=MzI4NDMyNTU2Mw==&mid=2247483695&idx=1&sn=91ea422913fc62579e020e941d1d059e#rd， 微信生产级 paxos 服务的实现原理

[5]  http://amberonrails.com/paxosmulti-paxos-algorithm/， 比较简单明了的介绍了 paxos