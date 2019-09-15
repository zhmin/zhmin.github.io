---
title: Zookeeper Zab 协议
date: 2019-09-15 13:25:39
tags: zookeeper, zab
categories: zookeeper
---



## 前言

Zookeeper 作为分布式的一致框架，提供了原子性的写，而且好保证了高可用（集群有过半节点正常运行）。这些特点被广泛应用，比如 Hbase 用来存储配置信息，还用来保证主从切换。本篇文章介绍了 zookeeper 的核心算法 zab 协议，zab 协议是由所有节点必须遵守的，它们协同工作实现了分布式的一致性。



## 集群状态

zab 协议保证了集群在任何时刻，要么只有一个 leader，要么没有 leader。这两种情况称为广播模式和恢复模式：

1. 处于广播模式的集群，能够正常对外提供服务，客户端可以正常的读写。
2. 处于恢复模式的集群，不能对外提供服务，这时节点有可能处于选举过程，同步过程。



广播模式比较简单，就是和二阶段提交类型，但是允许不超过半数的服务挂掉。客户端的写入请求都会转发给 leader ，然后 leader 将消息发送给 follower，如果超过半数的follower响应成功，那么就提交 commit 操作，将数据持久化到磁盘。

恢复模式有一点复杂，它会从集群中选举出一个节点作为 leader，这个节点需要具有最新的数据。而且这个 leader 需要被过半节点认同。我们回想下 zookeeper 的写操作需要过半的节点完成才能认为成功，而选举也需要过半的节点都同意，那么两个集合之间必定有交叉，就可以保证选举出来的是有最新数据的节点。

当选举完成后，follower 会向 leader 同步数据。当多数 follower 同步完成后，就进入到广播模式。



## 启动过程

任何节点启动都遵循着同样的过程，加载数据，选举，同步，处理请求。

<img src="zookeeper-startup.svg">

加载过程：zookeeper 的节点会在本地磁盘持久化数据，启动时会加载这些本地数据，这些数据都是根据事务 id 顺序存储的，这样就可以根据最大的事务 id 来判断数据的新旧

选举过程：各个节点相同通信，选举出一个共同的 leader 节点。这个 leader 节点的选票必须过半，并且有着最新的数据。

同步过程：当成为 follower 节点后，需要向 leader 节点同步数据，因为 follower 节点的数据可能落后于leader。

处理请求：当同步过程完成后，集群就可以对外提供读写服务了。



下面是节点运行的程序，可以看到节点在不同的时刻，会有着不同的状态。LOOKING 状态表示处于选举阶段，FOLLOWING 状态表示节点变为 follower 角色，而 LEADING 状态表示节点成为 leader 角色。

```java
public class QuorumPeer extends ZooKeeperThread implements QuorumStats.Provider {
    
    @Override
    public void run() {
        while (running) {
            switch (getPeerState()) {
            case LOOKING:
                 // 执行选举，根据返回leader地址，设置自身为follower还是leader
                 setCurrentVote(makeLEStrategy().lookForLeader());
                 break;
            case FOLLOWING:
                 try {
                     // 执行follower程序
                     setFollower(makeFollower(logFactory));
                     follower.followLeader();
                 } finally {
                     // 如果follower程序发生异常或退出
                     follower.shutdown();
                     setFollower(null);
                     // 设置状态为LOOKING
                     updateServerState();
                 }
                 break;
            case LEADING:
                 try {
                     // 执行leader程序
                     setLeader(makeLeader(logFactory));
                     leader.lead();
                     setLeader(null);
                 } finally {
                     // 如果leader程序发生异常或退出，设置状态为LOOKING
                     setLeader(null);
                     updateServerState();
                 }
            }
        }
    }
}   
```







## 通信组件

zookeeper 集群有着多个节点，节点之间的通信都是通过网络。zookeeper 使用队列存储请求和响应，并且对于每个连接都有单独的读队列和写队列，同时还有单独的读线程负责解析请求，单独的写线程负责发送数据。

<img src="zookeeper-network.svg">





## 选举框架

zookeeper 的选举算法有多种，不过目前主要只使用 FastLeaderElection 算法，其余的已经被废弃了。FastLeaderElection 在通信组件的基础上还做了一层封装，它有两个线程，读线程 WorkerReceiver  和写线程 WorkerSender。读线程负责从通信组件的队列里读取请求，并且解析成选票请求，添加到投票请求队列。写线程负责把要发送的投票添加到通信组件的写队列里。

<img src="zookeeper-vote-flow.svg">



## FastLeaderElection  选举算法

FastLeaderElection  选举会根据对方的状态来处理选票的，如果对方是 leader 或者 follower，那么说明集群中已经存在一个 leader了，这时只需要检测这个 leader 是否被过半节点认同。如果对方是looking 状态，那么需要比较是否处于同一轮选举，还有选票的大小。如果对方的选票比自身大，需要更改自身的选票，并且向其他节点发出通知。

下面先来看看选票包含了哪些重要信息

```java
public class Vote {
    // 选择哪个节点作为leader
    final private long id;
    // 选定leader节点的最大事务id，越大说明数据越新
    final private long zxid;
    // 该投票属于第几轮选举
    final private long electionEpoch;
    // leader节点的epoch
    final private long peerEpoch;
}
```

接下来看看选举原理，

```java
public class FastLeaderElection implements Election {
    AtomicLong logicalclock = new AtomicLong();  // 选举轮数
    
    // 自身节点的投票信息
    long proposedLeader;  // 哪个节点为leader
    long proposedZxid;    // 选举的leader节点的最大事务id
    long proposedEpoch;   // 选举的leader节点的最大epoch
    
    public Vote lookForLeader() throws InterruptedException {
        // 存储着来自同选举轮数，每个节点对应的投票，投票里包含了选举哪个节点作为leader
        Map<Long, Vote> recvset = new HashMap<Long, Vote>();
        // 存储着来自leader或者follower的投票
        Map<Long, Vote> outofelection = new HashMap<Long, Vote>();
        // 每次选举都会增加选举轮数
        logicalclock.incrementAndGet();
        // 设置自身的投票信息
        updateProposal(getInitId(), getInitLastLoggedZxid(), getPeerEpoch());
        // 发送投票给其余的节点
        sendNotifications();
        
        // 循环，一直到选出leader为止
        while ((self.getPeerState() == ServerState.LOOKING) && (!stop)) {
            // 从队列里获取其余节点的投票信息
            Notification n = recvqueue.poll(notTimeout, TimeUnit.MILLISECONDS);
            // 根据发送投票的节点的状态，做不同的处理
            switch (n.state) {
            case LOOKING:
                 // 发送节点的状态为LOOKING
                 // 先比较选举轮数，再比较epoch和事务id
                 if (n.electionEpoch > logicalclock.get()) {
                     // 如果自己的选举轮数落后，更新自己的选举轮数
                     logicalclock.set(n.electionEpoch);
                     // 还需要情况旧的投票信息，因为这些都已经过时了
                     recvset.clear();
                     // 比较谁更适合当选leader，如果是别的节点，那么就更新自己的投票信息
                     if (totalOrderPredicate(n.leader, n.zxid, n.peerEpoch, getInitId(), getInitLastLoggedZxid(), getPeerEpoch())) {
                         updateProposal(n.leader, n.zxid, n.peerEpoch);
                     } else {
                         updateProposal(getInitId(), getInitLastLoggedZxid(), getPeerEpoch());
                     }
                     // 需要重新发送投票，因为之前的已经过时了
                     sendNotifications();
                 } else if (n.electionEpoch < logicalclock.get()) {
                     // 如果发送投票的节点，选举轮数落后，那么就不用理睬
                     break;
                 } else if (totalOrderPredicate(n.leader, n.zxid, n.peerEpoch, proposedLeader, proposedZxid, proposedEpoch)) {
                     // 如果别的节点更适合当选leader，那么就更新自己的投票信息，并且重新发送
                     updateProposal(n.leader, n.zxid, n.peerEpoch);
                     sendNotifications();
                 }
                 
                 // 记录投票信息
                 recvset.put(n.sid, new Vote(n.leader, n.zxid, n.electionEpoch, n.peerEpoch));
                
                 // 如果有超过半数的投票一致，那么有可能新的leader被选举出来了
                if (voteSet.hasAllQuorums()) {
                    // 检测队列里的剩余投票请求，并且直到等待finalizeWait时间，仍然没有新的请求
                    // 如果有别的节点更适合当选leader，那么就跳到循环开始，来处理投票信息
                    while ((n = recvqueue.poll(finalizeWait, TimeUnit.MILLISECONDS)) != null) {
                        if (totalOrderPredicate(n.leader, n.zxid, n.peerEpoch, proposedLeader, proposedZxid, proposedEpoch)) {
                            recvqueue.put(n);
                            break;
                        }
                    }
                    // 如果仍然没有新请求，那么就认为leader选定出来了
                    // 根据比较节点id，判断是follower角色还是leader角色
                    if (n == null) {
                        // 这里会更新该节点的状态为follower或者leader
                        setPeerState(proposedLeader, voteSet);
                        // 返回最终的选票
                        Vote endVote = new Vote(proposedLeader, proposedZxid, logicalclock.get(), proposedEpoch);
                        return endVote;
                    }
                }
                break;
           
            case FOLLOWING:
            case LEADING:
                if(n.electionEpoch == logicalclock.get()){
                    // 在同一选举轮数中，已经有leader产生并且开始运行了
                    recvset.put(n.sid, new Vote(n.leader, n.zxid, n.electionEpoch, n.peerEpoch));
                    if (voteSet.hasAllQuorums() && checkLeader(outofelection, n.leader, n.electionEpoch)) {
                        // 这里会更新该节点的状态为follower或者leader
                        setPeerState(n.leader, voteSet);
                        Vote endVote = new Vote(n.leader, n.zxid, n.electionEpoch, n.peerEpoch);
                        return endVote;
                    }
                }
                // 这里不用区分选举轮数，因为follower和leader角色已经稳定运行了
                outofelection.put(n.sid, new Vote(n.version, n.leader,
                                n.zxid, n.electionEpoch, n.peerEpoch, n.state));
                // 如果多数节点的投票相同，那么检查该leader节点是否处于leader状态
                if (voteSet.hasAllQuorums() && checkLeader(outofelection, n.leader, n.electionEpoch)) {
                    // 设置选举轮数
                    logicalclock.set(n.electionEpoch);
                    // 这里会更新该节点的状态为follower或者leader
                    setPeerState(n.leader, voteSet);
                    // 返回最终的选票
                	Vote endVote = new Vote(n.leader, n.zxid, n.electionEpoch, n.peerEpoch);
                	return endVote;
                }
           		break;
        }
    }
}
                    
            

```





我们再来看看 leader 角色或者 follower 角色是如何处理投票请求的。在 WorkerReceiver 线程里，会检查选票请求。如果自身是 leader 或者 follower，会直接返回自身的选票 。如果对方是 LOOKING 状态，那么就会将选票添加到队列里，等待选举算法处理。

```java
class WorkerReceiver extends ZooKeeperThread {
    
    QuorumCnxManager manager;
    
    public void run() {
    	Message response;
        while (!stop) {
            response = manager.pollRecvQueue(3000, TimeUnit.MILLISECONDS);
            Notification n = new Notification();
            
            // 解析响应，生成Notification实例
            ......
            
            if (self.getPeerState() == QuorumPeer.ServerState.LOOKING) {
                // 如果自身节点是LOOKING状态，那么将请求添加到队列里，等待算法处理
                recvqueue.offer(n);
                // ackstate表示发送该响应的节点的状态，如果对方状态也是LOOKING，并且选举轮数落后，
                // 那么将自身的选票返回给它
                if ((ackstate == QuorumPeer.ServerState.LOOKING) && (n.electionEpoch < logicalclock.get())) {
                    // 获取自身节点的选票
                    Vote v = getVote();
                    // 构建响应
                    ToSend notmsg = new ToSend(
                        ToSend.mType.notification,
                        v.getId(),
                        v.getZxid(),
                        logicalclock.get(),
                        self.getPeerState(),
                        response.sid,
                        v.getPeerEpoch(),
                        qv.toString().getBytes());
                    // 添加到队列，由后台线程发送
                    sendqueue.offer(notmsg);
                }
            } else {
                // 如果自身节点是leader或者follower，那么表明集群中已经有了leader节点了
                Vote current = self.getCurrentVote();
                if (ackstate == QuorumPeer.ServerState.LOOKING) {
                    // 直接返回自身节点的选票
                    ToSend notmsg = new ToSend(
                        ToSend.mType.notification,
                        current.getId(),
                        current.getZxid(),
                        current.getElectionEpoch(),
                        self.getPeerState(),
                        response.sid,
                        current.getPeerEpoch(),
                        qv.toString().getBytes());
                    // 添加到队列，由后台线程发送
                    sendqueue.offer(notmsg);
                }
            }
        }
    }
}          
```





选举完成后，各个节点的角色都已经确定好了，接下来继续看 leader 和 follower 角色的运行原理。



## 同步

同步的原理很简单，目的是为了让 follower 和 leader 的数据必须保持一致。通信过程如下：

<img src="zookeeper-sync.svg">



如果 follower 节点落后太多，则需要全量同步来提高效率，返回 SNAP 响应。

如果落后不是太多，则选择增量同步，返回 DIFF 响应。





## 处理写请求



leader 节点接收到客户端的写请求，会先向follower角色（至少要保证过半的节点存活）发送 PROPOSAL 请求。

follower节点收到 PROPOSAL 请求后，会将此次请求存在内存中，然后返回 ACK 响应。

leader 如果有超过半数的节点成功响应 ACK，那么就会向 follower 节点发送COMMIT请求。

follower 节点收到 COMMIT请求后，会将内存的请求，持久化到磁盘中，表示数据成功写入。



