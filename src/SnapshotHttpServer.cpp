/*
 * mod-rare-tracker: a tiny read-only HTTP server. See SnapshotHttpServer.h.
 *
 * Plain boost.asio (AzerothCore already depends on it). One request per connection, GET and HEAD
 * only, request headers capped at 8 KB, and every connection is dropped after 5 seconds, so a
 * slow or hostile client can't tie it up.
 *
 * Released under the MIT License.
 */

#include "SnapshotHttpServer.h"

#include "Log.h"

#include <boost/asio/io_context.hpp>
#include <boost/asio/ip/tcp.hpp>
#include <boost/asio/steady_timer.hpp>
#include <boost/asio/write.hpp>

#include <array>
#include <chrono>
#include <ctime>

namespace
{
    using boost::asio::ip::tcp;

    constexpr std::size_t MAX_REQUEST_BYTES = 8 * 1024;
    constexpr auto CONNECTION_TIMEOUT = std::chrono::seconds(5);

    char const* EMPTY_SNAPSHOT = "{\"generated\":0,\"rares\":[]}";

    class Connection : public std::enable_shared_from_this<Connection>
    {
    public:
        Connection(tcp::socket socket, SnapshotHttpServer& server)
            : _socket(std::move(socket)), _timer(_socket.get_executor()), _server(server) { }

        void Start()
        {
            _timer.expires_after(CONNECTION_TIMEOUT);
            _timer.async_wait([self = shared_from_this()](boost::system::error_code const& error)
            {
                if (!error)
                {
                    boost::system::error_code ignored;
                    self->_socket.close(ignored);
                }
            });

            Read();
        }

    private:
        void Read()
        {
            _socket.async_read_some(boost::asio::buffer(_buffer),
                [self = shared_from_this()](boost::system::error_code const& error, std::size_t bytes)
                {
                    if (error)
                        return self->Close();

                    self->_request.append(self->_buffer.data(), bytes);
                    if (self->_request.size() > MAX_REQUEST_BYTES)
                        return self->Send(431, "Request Header Fields Too Large", "text/plain", "too large\n", false);

                    if (self->_request.find("\r\n\r\n") != std::string::npos)
                        return self->Respond();

                    self->Read();
                });
        }

        void Respond()
        {
            // Request line: METHOD SP TARGET SP VERSION
            std::size_t lineEnd = _request.find("\r\n");
            std::string line = _request.substr(0, lineEnd);
            std::size_t firstSpace = line.find(' ');
            std::size_t secondSpace = firstSpace == std::string::npos ? std::string::npos : line.find(' ', firstSpace + 1);
            if (secondSpace == std::string::npos)
                return Send(400, "Bad Request", "text/plain", "bad request\n", true);

            std::string method = line.substr(0, firstSpace);
            std::string target = line.substr(firstSpace + 1, secondSpace - firstSpace - 1);
            if (std::size_t query = target.find('?'); query != std::string::npos)
                target.resize(query);

            bool head = method == "HEAD";
            if (method != "GET" && !head)
                return Send(405, "Method Not Allowed", "text/plain", "GET only\n", head);

            if (target == "/" || target == "/rares.json")
            {
                _server.MarkRequested();
                std::shared_ptr<std::string const> snapshot = _server.Snapshot();
                return Send(200, "OK", "application/json; charset=utf-8", snapshot ? *snapshot : EMPTY_SNAPSHOT, head);
            }

            if (target == "/health")
                return Send(200, "OK", "text/plain", "ok\n", head);

            Send(404, "Not Found", "text/plain", "not found\n", head);
        }

        void Send(uint32_t status, char const* reason, char const* contentType, std::string const& body, bool headOnly)
        {
            _response = "HTTP/1.1 " + std::to_string(status) + " " + reason + "\r\n";
            _response += "Content-Type: " + std::string(contentType) + "\r\n";
            _response += "Content-Length: " + std::to_string(body.size()) + "\r\n";
            _response += "Cache-Control: no-store\r\n";
            if (!_server.AllowOrigin().empty())
                _response += "Access-Control-Allow-Origin: " + _server.AllowOrigin() + "\r\n";
            _response += "Connection: close\r\n\r\n";
            if (!headOnly)
                _response += body;

            boost::asio::async_write(_socket, boost::asio::buffer(_response),
                [self = shared_from_this()](boost::system::error_code const& /*error*/, std::size_t /*bytes*/)
                {
                    self->Close();
                });
        }

        void Close()
        {
            boost::system::error_code ignored;
            _socket.shutdown(tcp::socket::shutdown_both, ignored);
            _socket.close(ignored);
            _timer.cancel();
        }

        tcp::socket _socket;
        boost::asio::steady_timer _timer;
        SnapshotHttpServer& _server;
        std::array<char, 2048> _buffer{};
        std::string _request;
        std::string _response;
    };

    class Listener : public std::enable_shared_from_this<Listener>
    {
    public:
        Listener(boost::asio::io_context& io, tcp::endpoint const& endpoint, SnapshotHttpServer& server)
            : _acceptor(io), _server(server)
        {
            _acceptor.open(endpoint.protocol());
            _acceptor.set_option(tcp::acceptor::reuse_address(true));
            _acceptor.bind(endpoint);
            _acceptor.listen();
        }

        void Accept()
        {
            _acceptor.async_accept([self = shared_from_this()](boost::system::error_code const& error, tcp::socket socket)
            {
                if (error == boost::asio::error::operation_aborted)
                    return;

                if (!error)
                    std::make_shared<Connection>(std::move(socket), self->_server)->Start();

                self->Accept();
            });
        }

    private:
        tcp::acceptor _acceptor;
        SnapshotHttpServer& _server;
    };
}

SnapshotHttpServer::SnapshotHttpServer() = default;

SnapshotHttpServer::~SnapshotHttpServer()
{
    Stop();
}

bool SnapshotHttpServer::Start(std::string const& address, uint16_t port, std::string const& allowOrigin)
{
    if (IsRunning())
        return true;

    _allowOrigin = allowOrigin;
    _io = std::make_unique<boost::asio::io_context>(1);

    try
    {
        tcp::endpoint endpoint(boost::asio::ip::make_address(address), port);
        std::make_shared<Listener>(*_io, endpoint, *this)->Accept();
    }
    catch (std::exception const& e)
    {
        LOG_ERROR("module", "mod-rare-tracker: can't listen on {}:{}: {}", address, port, e.what());
        _io.reset();
        return false;
    }

    _thread = std::thread([this]()
    {
        for (;;)
        {
            try
            {
                _io->run();
                return;
            }
            catch (std::exception const& e)
            {
                LOG_ERROR("module", "mod-rare-tracker: HTTP server error: {}", e.what());
            }
        }
    });

    LOG_INFO("server.loading", ">> mod-rare-tracker: serving rares on http://{}:{}/rares.json", address, port);
    return true;
}

void SnapshotHttpServer::Stop()
{
    if (!_io)
        return;

    _io->stop();
    if (_thread.joinable())
        _thread.join();
    _io.reset();
}

void SnapshotHttpServer::Publish(std::string json)
{
    auto snapshot = std::make_shared<std::string const>(std::move(json));
    std::lock_guard<std::mutex> guard(_snapshotLock);
    _snapshot = std::move(snapshot);
}

std::shared_ptr<std::string const> SnapshotHttpServer::Snapshot() const
{
    std::lock_guard<std::mutex> guard(_snapshotLock);
    return _snapshot;
}

void SnapshotHttpServer::MarkRequested()
{
    _lastRequest.store(int64_t(std::time(nullptr)), std::memory_order_relaxed);
}
