# Certificate handling

Following the ZMQ ["ironhouse" exampe]("https://github.com/zeromq/pyzmq/blob/main/examples/security/ironhouse.py"), we verify both clients and servers with CurveZMQ (elliptic curve cryptography) with the hopes that this makes it reasonably safe to open the corresponding port on the server pc. 
(Should technically be fine within a trusted network anyway...).

Consequently, annoying key handling has to be done manually:

- Use generate_keys.py to create the required private/public keypair, which will be stored under ./mine/ within this directory.
(Try not getting them commited to git, please).

- If the name differs from the default (CLI argument: --name), it can be set in the options.json file 
(May be desirable to have the same name for one public key everywhere including host for better identification, idk).

- The public key from ./mine/ must be distributed among all the devices that should be able to conncet to it.
Vice versa, the public keys of all trusted devices must be made available in ./others/.
