/*
 * mod-rare-tracker: a tiny read-only HTTP server.
 *
 * It answers GET requests from its own thread with whatever snapshot the world thread last
 * published, so serving a request never touches game state. It also remembers when it was last
 * asked, which lets the world thread skip building snapshots while nobody is looking.
 *
 * Released under the MIT License.
 */

#ifndef MOD_RARE_TRACKER_SNAPSHOT_HTTP_SERVER_H
#define MOD_RARE_TRACKER_SNAPSHOT_HTTP_SERVER_H

#include <atomic>
#include <cstdint>
#include <memory>
#include <mutex>
#include <string>
#include <thread>

namespace boost::asio
{
    class io_context;
}

class SnapshotHttpServer
{
public:
    SnapshotHttpServer();
    ~SnapshotHttpServer();

    SnapshotHttpServer(SnapshotHttpServer const&) = delete;
    SnapshotHttpServer& operator=(SnapshotHttpServer const&) = delete;

    // Starts listening on its own thread. Returns false (and logs why) if the address can't be
    // bound. allowOrigin is sent as Access-Control-Allow-Origin; empty leaves the header out.
    bool Start(std::string const& address, uint16_t port, std::string const& allowOrigin);
    void Stop();
    bool IsRunning() const { return _thread.joinable(); }

    // Called from the world thread with a complete JSON document.
    void Publish(std::string json);

    // Unix time of the last request for the snapshot, 0 if there hasn't been one.
    int64_t LastRequestTime() const { return _lastRequest.load(std::memory_order_relaxed); }

    // Used by the connection handlers.
    std::shared_ptr<std::string const> Snapshot() const;
    void MarkRequested();
    std::string const& AllowOrigin() const { return _allowOrigin; }

private:
    std::unique_ptr<boost::asio::io_context> _io;
    std::thread _thread;
    std::string _allowOrigin;

    mutable std::mutex _snapshotLock;
    std::shared_ptr<std::string const> _snapshot;
    std::atomic<int64_t> _lastRequest{0};
};

#endif
