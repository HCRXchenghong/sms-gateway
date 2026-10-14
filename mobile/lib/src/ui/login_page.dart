/// Login screen. Asks for the server address because the gateway is
/// self-hosted and every customer runs their own endpoint.
library;

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../data/session_store.dart';
import '../state/app_scope.dart';

class LoginPage extends StatefulWidget {
  const LoginPage({super.key});

  @override
  State<LoginPage> createState() => _LoginPageState();
}

class _LoginPageState extends State<LoginPage> {
  final _formKey = GlobalKey<FormState>();

  /// host[:port] with an optional scheme and path; spaces and stray text fail.
  static final RegExp _serverPattern =
      RegExp(r'^(https?://)?[A-Za-z0-9._-]+(:\d+)?(/\S*)?$');

  late final TextEditingController _server;
  late final TextEditingController _username;
  final TextEditingController _password = TextEditingController();
  final TextEditingController _code = TextEditingController();
  bool _obscure = true;
  bool _busy = false;
  bool _needTotp = false;

  @override
  void initState() {
    super.initState();
    final store = context.read<AppScope>().store;
    _server = TextEditingController(text: store.serverUrl ?? '');
    _username = TextEditingController(text: store.lastUsername ?? '');
  }

  @override
  void dispose() {
    _server.dispose();
    _username.dispose();
    _password.dispose();
    _code.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    if (!(_formKey.currentState?.validate() ?? false)) return;
    setState(() => _busy = true);
    final scope = context.read<AppScope>();
    final ok = await scope.login(
      serverUrl: _server.text,
      username: _username.text.trim(),
      password: _password.text,
      code: _code.text.trim(),
    );
    if (!mounted) return;
    setState(() {
      _busy = false;
      _needTotp = !ok && (scope.loginError ?? '').contains('totp');
    });
    if (!ok) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(scope.loginError ?? '登录失败')),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Scaffold(
      body: SafeArea(
        child: Center(
          child: SingleChildScrollView(
            padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 32),
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 420),
              child: Form(
                key: _formKey,
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    Icon(Icons.cell_tower, size: 52, color: scheme.primary),
                    const SizedBox(height: 12),
                    Text('VoiceSMS Gateway',
                        textAlign: TextAlign.center,
                        style: Theme.of(context).textTheme.headlineSmall),
                    const SizedBox(height: 4),
                    Text('4G 语音短信网关',
                        textAlign: TextAlign.center,
                        style: Theme.of(context).textTheme.bodyMedium?.copyWith(color: scheme.onSurfaceVariant)),
                    const SizedBox(height: 32),
                    TextFormField(
                      controller: _server,
                      keyboardType: TextInputType.url,
                      autocorrect: false,
                      decoration: const InputDecoration(
                        labelText: '服务器地址',
                        hintText: 'https://gw.example.com:8443',
                        prefixIcon: Icon(Icons.dns_outlined),
                      ),
                      validator: (v) {
                        final value = (v ?? '').trim();
                        if (value.isEmpty) return '请填写服务器地址';
                        // Uri.parse percent-encodes spaces, so "not a url" would
                        // otherwise pass a host check. Match the shape first.
                        if (!_serverPattern.hasMatch(value)) return '地址格式不正确';
                        final uri = Uri.tryParse(SessionStore.normalizeUrl(value));
                        if (uri == null || uri.host.isEmpty) return '地址格式不正确';
                        return null;
                      },
                      onFieldSubmitted: (_) => _submit(),
                    ),
                    const SizedBox(height: 14),
                    TextFormField(
                      controller: _username,
                      autocorrect: false,
                      textInputAction: TextInputAction.next,
                      decoration: const InputDecoration(
                        labelText: '账号',
                        prefixIcon: Icon(Icons.person_outline),
                      ),
                      validator: (v) => (v ?? '').trim().isEmpty ? '请填写账号' : null,
                    ),
                    const SizedBox(height: 14),
                    TextFormField(
                      controller: _password,
                      obscureText: _obscure,
                      textInputAction: _needTotp ? TextInputAction.next : TextInputAction.done,
                      decoration: InputDecoration(
                        labelText: '密码',
                        prefixIcon: const Icon(Icons.lock_outline),
                        suffixIcon: IconButton(
                          icon: Icon(_obscure ? Icons.visibility_off : Icons.visibility),
                          onPressed: () => setState(() => _obscure = !_obscure),
                        ),
                      ),
                      validator: (v) => (v ?? '').isEmpty ? '请填写密码' : null,
                      onFieldSubmitted: (_) {
                        if (!_needTotp) _submit();
                      },
                    ),
                    if (_needTotp) ...[
                      const SizedBox(height: 14),
                      TextFormField(
                        controller: _code,
                        keyboardType: TextInputType.number,
                        maxLength: 6,
                        decoration: const InputDecoration(
                          labelText: '动态验证码',
                          prefixIcon: Icon(Icons.shield_outlined),
                          counterText: '',
                        ),
                        onFieldSubmitted: (_) => _submit(),
                      ),
                    ],
                    const SizedBox(height: 24),
                    FilledButton(
                      onPressed: _busy ? null : _submit,
                      child: _busy
                          ? const SizedBox(
                              height: 20, width: 20, child: CircularProgressIndicator(strokeWidth: 2))
                          : const Text('登录'),
                    ),
                    const SizedBox(height: 16),
                    Text(
                      '账号与密码由网关后台管理员分配。忘记密码请联系管理员在后台重置。',
                      textAlign: TextAlign.center,
                      style: Theme.of(context)
                          .textTheme
                          .bodySmall
                          ?.copyWith(color: scheme.onSurfaceVariant),
                    ),
                  ],
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}

