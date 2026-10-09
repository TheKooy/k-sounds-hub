#include <QByteArray>
#include <QCoreApplication>
#include <QCommandLineParser>
#include <QDBusArgument>
#include <QDBusConnection>
#include <QDBusInterface>
#include <QDBusMessage>
#include <QDBusMetaType>
#include <QDBusObjectPath>
#include <QEventLoop>
#include <QList>
#include <QMetaType>
#include <QObject>
#include <QSocketNotifier>
#include <QString>
#include <QTextStream>
#include <QTimer>
#include <QVariant>
#include <QVariantMap>

#include <functional>
#include <unistd.h>

static constexpr auto kPortalService = "org.freedesktop.portal.Desktop";
static constexpr auto kPortalPath = "/org/freedesktop/portal/desktop";
static constexpr auto kPortalInterface = "org.freedesktop.portal.GlobalShortcuts";
static constexpr auto kRegistryInterface = "org.freedesktop.host.portal.Registry";
static constexpr auto kRequestInterface = "org.freedesktop.portal.Request";
static constexpr auto kSessionInterface = "org.freedesktop.portal.Session";

struct ShortcutSpec {
    QString id;
    QVariantMap properties;
};
Q_DECLARE_METATYPE(ShortcutSpec)

using ShortcutSpecs = QList<ShortcutSpec>;
Q_DECLARE_METATYPE(ShortcutSpecs)

QDBusArgument &operator<<(QDBusArgument &argument, const ShortcutSpec &spec)
{
    argument.beginStructure();
    argument << spec.id << spec.properties;
    argument.endStructure();
    return argument;
}

const QDBusArgument &operator>>(const QDBusArgument &argument, ShortcutSpec &spec)
{
    argument.beginStructure();
    argument >> spec.id >> spec.properties;
    argument.endStructure();
    return argument;
}

class RequestWaiter final : public QObject {
    Q_OBJECT

public:
    explicit RequestWaiter(QObject *parent = nullptr) : QObject(parent) {}

    bool wait(
        QDBusConnection &bus,
        const QString &requestPath,
        int timeoutMs,
        uint *responseOut,
        QVariantMap *resultsOut,
        QString *errorOut)
    {
        QEventLoop loop;
        QTimer timer;
        timer.setSingleShot(true);

        bool received = false;
        uint response = 2;
        QVariantMap results;

        const bool connected = bus.connect(
            kPortalService,
            requestPath,
            kRequestInterface,
            QStringLiteral("Response"),
            this,
            SLOT(onResponse(uint,QVariantMap))
        );
        if (!connected) {
            if (errorOut) {
                *errorOut = QStringLiteral("could not connect Request.Response");
            }
            return false;
        }

        m_callback = [&](uint code, const QVariantMap &payload) {
            received = true;
            response = code;
            results = payload;
            loop.quit();
        };

        if (timeoutMs > 0) {
            QObject::connect(&timer, &QTimer::timeout, &loop, &QEventLoop::quit);
            timer.start(timeoutMs);
        }
        loop.exec();

        bus.disconnect(
            kPortalService,
            requestPath,
            kRequestInterface,
            QStringLiteral("Response"),
            this,
            SLOT(onResponse(uint,QVariantMap))
        );
        m_callback = {};

        if (!received) {
            if (errorOut) {
                *errorOut = QStringLiteral("timed out waiting for Request.Response");
            }
            return false;
        }

        if (responseOut) {
            *responseOut = response;
        }
        if (resultsOut) {
            *resultsOut = results;
        }
        return true;
    }

public slots:
    void onResponse(uint response, const QVariantMap &results)
    {
        if (m_callback) {
            m_callback(response, results);
        }
    }

private:
    std::function<void(uint, const QVariantMap &)> m_callback;
};

class PortalHelper final : public QObject {
    Q_OBJECT

public:
    PortalHelper(
        const QString &appId,
        const QString &shortcutId,
        const QString &description,
        const QString &trigger,
        bool probeOnly,
        QObject *parent = nullptr)
        : QObject(parent),
          m_appId(appId),
          m_shortcutId(shortcutId),
          m_description(description),
          m_trigger(trigger),
          m_probeOnly(probeOnly),
          m_bus(QDBusConnection::sessionBus())
    {
    }

    int start()
    {
        if (!m_bus.isConnected()) {
            fail(QStringLiteral("D-Bus session bus unavailable"));
            return 2;
        }

        qDBusRegisterMetaType<ShortcutSpec>();
        qDBusRegisterMetaType<ShortcutSpecs>();

        if (!registerHost()) {
            return 3;
        }
        if (!createSession()) {
            return 4;
        }
        if (!connectSignals()) {
            closeSession();
            return 5;
        }
        if (!listShortcuts()) {
            closeSession();
            return 6;
        }

        // KDE is authoritative. Merely starting K-Sounds must never recreate a
        // shortcut the user disabled/removed in System Settings. Existing KDE
        // state is loaded above; registration of a missing action only happens
        // after an explicit SETUP command from K-Sounds.
        QTextStream(stdout)
            << "READY\t" << m_shortcutId << "\t" << m_trigger << Qt::endl;
        reportSync();

        if (m_probeOnly) {
            closeSession();
            return 0;
        }

        setupStdinCommands();
        return -1;
    }

public slots:
    void onActivated(
        const QDBusObjectPath &session,
        const QString &shortcutId,
        qulonglong,
        const QVariantMap &)
    {
        if (session.path() != m_sessionHandle || shortcutId != m_shortcutId) {
            return;
        }
        if (m_kdeEnabled) {
            QTextStream(stdout) << "ACTIVATED\t" << shortcutId << Qt::endl;
        }
    }

    void onDeactivated(
        const QDBusObjectPath &session,
        const QString &shortcutId,
        qulonglong,
        const QVariantMap &)
    {
        if (session.path() != m_sessionHandle || shortcutId != m_shortcutId) {
            return;
        }
        if (m_kdeEnabled) {
            QTextStream(stdout) << "DEACTIVATED\t" << shortcutId << Qt::endl;
        }
    }

    void onShortcutsChanged(
        const QDBusObjectPath &session,
        const ShortcutSpecs &shortcuts)
    {
        if (session.path() != m_sessionHandle) {
            return;
        }
        applyShortcutState(shortcuts);
        reportSync();
    }

    void onStdinActivated()
    {
        char buffer[512];
        const ssize_t count = ::read(STDIN_FILENO, buffer, sizeof(buffer));
        if (count <= 0) {
            m_stdinNotifier->setEnabled(false);
            return;
        }

        m_stdinBuffer.append(buffer, static_cast<qsizetype>(count));
        while (true) {
            const qsizetype newline = m_stdinBuffer.indexOf('\n');
            if (newline < 0) {
                break;
            }
            QByteArray line = m_stdinBuffer.left(newline).trimmed();
            m_stdinBuffer.remove(0, newline + 1);
            const QString command = QString::fromUtf8(line).trimmed().toUpper();
            if (command == QStringLiteral("CONFIGURE")) {
                configureShortcuts();
            } else if (command == QStringLiteral("SETUP")) {
                bindShortcut();
            } else if (command == QStringLiteral("REFRESH")) {
                if (listShortcuts()) {
                    reportSync();
                }
            }
        }
    }

private:
    static QString requestPathFromReply(const QDBusMessage &reply)
    {
        if (reply.type() == QDBusMessage::ErrorMessage || reply.arguments().isEmpty()) {
            return {};
        }

        const QVariant value = reply.arguments().constFirst();
        if (value.canConvert<QDBusObjectPath>()) {
            return value.value<QDBusObjectPath>().path();
        }
        return value.toString();
    }

    static ShortcutSpecs shortcutSpecsFromVariant(const QVariant &value)
    {
        if (!value.isValid()) {
            return {};
        }
        if (value.canConvert<ShortcutSpecs>()) {
            return value.value<ShortcutSpecs>();
        }
        if (value.canConvert<QDBusArgument>()) {
            return qdbus_cast<ShortcutSpecs>(value.value<QDBusArgument>());
        }
        return {};
    }

    bool waitRequest(
        const QString &requestPath,
        int timeoutMs,
        QVariantMap *resultsOut,
        QString *errorOut,
        uint *responseOut = nullptr)
    {
        RequestWaiter waiter;
        uint response = 2;
        QVariantMap results;
        QString error;

        if (!waiter.wait(
                m_bus,
                requestPath,
                timeoutMs,
                &response,
                &results,
                &error)) {
            if (errorOut) {
                *errorOut = error;
            }
            return false;
        }

        if (responseOut) {
            *responseOut = response;
        }
        if (response != 0) {
            if (errorOut) {
                *errorOut = QStringLiteral("portal response=%1").arg(response);
            }
            return false;
        }

        if (resultsOut) {
            *resultsOut = results;
        }
        return true;
    }

    bool registerHost()
    {
        QDBusInterface registry(
            kPortalService,
            kPortalPath,
            kRegistryInterface,
            m_bus
        );

        if (!registry.isValid()) {
            return true;
        }

        const QDBusMessage reply = registry.call(
            QStringLiteral("Register"),
            m_appId,
            QVariantMap{}
        );

        if (reply.type() == QDBusMessage::ErrorMessage) {
            fail(QStringLiteral("Registry.Register: %1").arg(reply.errorMessage()));
            return false;
        }
        return true;
    }

    bool createSession()
    {
        QDBusInterface portal(
            kPortalService,
            kPortalPath,
            kPortalInterface,
            m_bus
        );

        if (!portal.isValid()) {
            fail(QStringLiteral("GlobalShortcuts portal unavailable"));
            return false;
        }

        const auto pid = QCoreApplication::applicationPid();

        QVariantMap options;
        options.insert(
            QStringLiteral("handle_token"),
            QStringLiteral("ksounds_create_%1").arg(pid)
        );
        options.insert(
            QStringLiteral("session_handle_token"),
            QStringLiteral("ksounds_session_%1").arg(pid)
        );

        const QDBusMessage reply = portal.call(
            QStringLiteral("CreateSession"),
            options
        );

        if (reply.type() == QDBusMessage::ErrorMessage) {
            fail(QStringLiteral("CreateSession call: %1").arg(reply.errorMessage()));
            return false;
        }

        const QString requestPath = requestPathFromReply(reply);
        if (requestPath.isEmpty()) {
            fail(QStringLiteral("CreateSession returned no request path"));
            return false;
        }

        QVariantMap results;
        QString error;
        if (!waitRequest(requestPath, 5000, &results, &error)) {
            fail(QStringLiteral("CreateSession response: %1").arg(error));
            return false;
        }

        const QVariant sessionValue = results.value(QStringLiteral("session_handle"));
        if (sessionValue.canConvert<QDBusObjectPath>()) {
            m_sessionHandle = sessionValue.value<QDBusObjectPath>().path();
        } else {
            m_sessionHandle = sessionValue.toString();
        }

        if (m_sessionHandle.isEmpty()) {
            fail(QStringLiteral("CreateSession returned no session_handle"));
            return false;
        }
        return true;
    }

    bool connectSignals()
    {
        const bool activatedConnected = m_bus.connect(
            kPortalService,
            kPortalPath,
            kPortalInterface,
            QStringLiteral("Activated"),
            this,
            SLOT(onActivated(QDBusObjectPath,QString,qulonglong,QVariantMap))
        );
        const bool deactivatedConnected = m_bus.connect(
            kPortalService,
            kPortalPath,
            kPortalInterface,
            QStringLiteral("Deactivated"),
            this,
            SLOT(onDeactivated(QDBusObjectPath,QString,qulonglong,QVariantMap))
        );
        const bool changedConnected = m_bus.connect(
            kPortalService,
            kPortalPath,
            kPortalInterface,
            QStringLiteral("ShortcutsChanged"),
            this,
            SLOT(onShortcutsChanged(QDBusObjectPath,ShortcutSpecs))
        );

        if (!activatedConnected || !deactivatedConnected || !changedConnected) {
            fail(QStringLiteral("could not connect GlobalShortcuts signals"));
            return false;
        }
        return true;
    }

    bool listShortcuts()
    {
        QDBusInterface portal(
            kPortalService,
            kPortalPath,
            kPortalInterface,
            m_bus
        );

        QVariantMap options;
        options.insert(
            QStringLiteral("handle_token"),
            QStringLiteral("ksounds_list_%1").arg(QCoreApplication::applicationPid())
        );

        const QDBusMessage reply = portal.call(
            QStringLiteral("ListShortcuts"),
            QVariant::fromValue(QDBusObjectPath(m_sessionHandle)),
            options
        );
        if (reply.type() == QDBusMessage::ErrorMessage) {
            fail(QStringLiteral("ListShortcuts call: %1").arg(reply.errorMessage()));
            return false;
        }

        const QString requestPath = requestPathFromReply(reply);
        if (requestPath.isEmpty()) {
            fail(QStringLiteral("ListShortcuts returned no request path"));
            return false;
        }

        QVariantMap results;
        QString error;
        if (!waitRequest(requestPath, 5000, &results, &error)) {
            fail(QStringLiteral("ListShortcuts response: %1").arg(error));
            return false;
        }

        applyShortcutState(
            shortcutSpecsFromVariant(results.value(QStringLiteral("shortcuts")))
        );
        return true;
    }

    bool bindShortcut()
    {
        if (m_bindAttempted) {
            return m_registered;
        }
        m_bindAttempted = true;

        QDBusInterface portal(
            kPortalService,
            kPortalPath,
            kPortalInterface,
            m_bus
        );

        ShortcutSpec spec;
        spec.id = m_shortcutId;
        spec.properties.insert(QStringLiteral("description"), m_description);
        if (!m_trigger.trimmed().isEmpty()) {
            spec.properties.insert(QStringLiteral("preferred_trigger"), m_trigger);
        }

        ShortcutSpecs specs;
        specs.append(spec);

        QVariantMap options;
        options.insert(
            QStringLiteral("handle_token"),
            QStringLiteral("ksounds_bind_%1").arg(QCoreApplication::applicationPid())
        );

        QTextStream(stdout)
            << "PENDING\t" << m_shortcutId << "\t" << m_trigger << Qt::endl;

        const QDBusMessage reply = portal.call(
            QStringLiteral("BindShortcuts"),
            QVariant::fromValue(QDBusObjectPath(m_sessionHandle)),
            QVariant::fromValue(specs),
            QString(),
            options
        );

        if (reply.type() == QDBusMessage::ErrorMessage) {
            QTextStream(stdout)
                << "SETUP_ERROR\t" << reply.errorMessage() << Qt::endl;
            listShortcuts();
            reportSync();
            return false;
        }

        const QString requestPath = requestPathFromReply(reply);
        if (requestPath.isEmpty()) {
            QTextStream(stdout) << "SETUP_ERROR\tBindShortcuts returned no request path" << Qt::endl;
            return false;
        }

        QVariantMap results;
        QString error;
        uint response = 2;
        const bool ok = waitRequest(requestPath, 0, &results, &error, &response);
        if (ok) {
            applyShortcutState(
                shortcutSpecsFromVariant(results.value(QStringLiteral("shortcuts")))
            );
        } else {
            // KDE may still have created/updated the action before a cancelled
            // dialog. Refresh from the authoritative component state.
            listShortcuts();
            QTextStream(stdout)
                << "SETUP_CANCELLED\t" << error << Qt::endl;
        }
        reportSync();
        return ok;
    }

    bool configureShortcuts()
    {
        if (!m_registered) {
            return bindShortcut();
        }

        QDBusInterface portal(
            kPortalService,
            kPortalPath,
            kPortalInterface,
            m_bus
        );
        const QDBusMessage reply = portal.call(
            QStringLiteral("ConfigureShortcuts"),
            QVariant::fromValue(QDBusObjectPath(m_sessionHandle)),
            QString(),
            QVariantMap{}
        );
        if (reply.type() == QDBusMessage::ErrorMessage) {
            QTextStream(stdout)
                << "CONFIGURE_ERROR\t" << reply.errorMessage() << Qt::endl;
            return false;
        }
        QTextStream(stdout) << "CONFIGURE_OPENED\t" << m_shortcutId << Qt::endl;
        return true;
    }

    void applyShortcutState(const ShortcutSpecs &shortcuts)
    {
        m_registered = false;
        m_kdeEnabled = false;
        m_kdeTrigger.clear();

        for (const auto &spec : shortcuts) {
            if (spec.id != m_shortcutId) {
                continue;
            }
            m_registered = true;
            m_kdeTrigger = spec.properties
                               .value(QStringLiteral("trigger_description"))
                               .toString()
                               .trimmed();
            m_kdeEnabled = !m_kdeTrigger.isEmpty();
            break;
        }
    }

    void reportSync()
    {
        QTextStream out(stdout);
        if (!m_registered) {
            out << "SYNC\t" << m_shortcutId << "\tMISSING\t" << Qt::endl;
        } else if (!m_kdeEnabled) {
            out << "SYNC\t" << m_shortcutId << "\tDISABLED\t" << Qt::endl;
        } else {
            out << "SYNC\t" << m_shortcutId << "\tENABLED\t" << m_kdeTrigger << Qt::endl;
        }
    }

    void setupStdinCommands()
    {
        m_stdinNotifier = new QSocketNotifier(STDIN_FILENO, QSocketNotifier::Read, this);

        // QSocketNotifier::activated is a private signal whose C++ signature
        // contains Qt's trailing QPrivateSignal.  Qt's meta-object signature
        // intentionally hides that implementation detail, so use the
        // string-based connection here.  This is compatible with Qt 6.11,
        // where selecting the apparent 2-argument PMF with qOverload fails.
        const bool connected = QObject::connect(
            m_stdinNotifier,
            SIGNAL(activated(QSocketDescriptor,QSocketNotifier::Type)),
            this,
            SLOT(onStdinActivated())
        );
        if (!connected) {
            QTextStream(stderr)
                << "ERROR\tstdin-notifier-connect-failed" << Qt::endl;
        }
    }

    void closeSession()
    {
        if (m_sessionHandle.isEmpty()) {
            return;
        }

        QDBusInterface session(
            kPortalService,
            m_sessionHandle,
            kSessionInterface,
            m_bus
        );

        if (session.isValid()) {
            session.call(QStringLiteral("Close"));
        }
        m_sessionHandle.clear();
    }

    void fail(const QString &message)
    {
        QTextStream(stdout) << "ERROR\t" << message << Qt::endl;
    }

private:
    QString m_appId;
    QString m_shortcutId;
    QString m_description;
    QString m_trigger;
    bool m_probeOnly = false;
    QDBusConnection m_bus;
    QString m_sessionHandle;
    bool m_registered = false;
    bool m_kdeEnabled = false;
    QString m_kdeTrigger;
    bool m_bindAttempted = false;
    QSocketNotifier *m_stdinNotifier = nullptr;
    QByteArray m_stdinBuffer;
};

int main(int argc, char **argv)
{
    QCoreApplication app(argc, argv);
    QCoreApplication::setApplicationName(
        QStringLiteral("K-Sounds Global Shortcuts Helper")
    );

    QCommandLineParser parser;
    parser.addHelpOption();

    QCommandLineOption appIdOption(
        QStringList{QStringLiteral("app-id")},
        QStringLiteral("Portal host application id"),
        QStringLiteral("id"),
        QStringLiteral("k-sounds-hub")
    );
    QCommandLineOption shortcutIdOption(
        QStringList{QStringLiteral("shortcut-id")},
        QStringLiteral("Stable shortcut id"),
        QStringLiteral("id")
    );
    QCommandLineOption descriptionOption(
        QStringList{QStringLiteral("description")},
        QStringLiteral("Shortcut description"),
        QStringLiteral("text")
    );
    QCommandLineOption triggerOption(
        QStringList{QStringLiteral("trigger")},
        QStringLiteral("XDG preferred trigger used only when explicitly setting up a missing action"),
        QStringLiteral("trigger")
    );
    QCommandLineOption probeOption(
        QStringList{QStringLiteral("probe")},
        QStringLiteral("Report current KDE state, close session and exit")
    );

    parser.addOption(appIdOption);
    parser.addOption(shortcutIdOption);
    parser.addOption(descriptionOption);
    parser.addOption(triggerOption);
    parser.addOption(probeOption);
    parser.process(app);

    const QString shortcutId = parser.value(shortcutIdOption).trimmed();
    const QString description = parser.value(descriptionOption).trimmed();
    const QString trigger = parser.value(triggerOption).trimmed();

    if (shortcutId.isEmpty() || description.isEmpty()) {
        QTextStream(stdout)
            << "ERROR\tmissing --shortcut-id or --description"
            << Qt::endl;
        return 64;
    }

    PortalHelper helper(
        parser.value(appIdOption).trimmed(),
        shortcutId,
        description,
        trigger,
        parser.isSet(probeOption)
    );

    const int result = helper.start();
    if (result >= 0) {
        return result;
    }

    return app.exec();
}

#include "main.moc"
