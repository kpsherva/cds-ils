..
    Copyright (C) 2018 CERN.

    CDS Books is free software; you can redistribute it and/or modify it
    under the terms of the MIT License; see LICENSE file for more details.

Installation
============

First, create a `virtualenv <https://virtualenv.pypa.io/en/stable/installation/>`_
using `virtualenvwrapper <https://virtualenvwrapper.readthedocs.io/en/latest/install.html>`_
in order to sandbox our Python environment for development:

.. code-block:: console

    $ mkvirtualenv cdsbooks

Install dependencies
--------------------

**Developers**

Then, install all required dependencies (use the `--src` if you want to define the path where to clone the
dependencies):

.. code-block:: console

    $ pip install -r requirements.developer.txt [--src my/modules/path]

Only the **first** time, install a few needed global `npm` dependencies:

.. code-block:: console

    $ npm update && npm install -g node-sass@4.9.0 clean-css@3.4.19 uglify-js@2.7.3 requirejs@2.2.0

Build the UI app:

.. code-block:: console

    (cdsbooks)$ cd my/modules/path/invenio-app-ils/
    (cdsbooks)$ ./scripts/build_assets

Tests
-----

.. code-block:: console

    (cdsbooks)$ ./run-tests.sh

Run
---

Run Invenio services on Docker:

.. code-block:: console

    $ docker-compose up

Only the first time, init DB and ES:

.. code-block:: console

    (cdsbooks)$ cd my/modules/path/invenio-app-ils/
    (cdsbooks)$ ./scripts/setup

Run Invenio:

.. code-block:: console

    (cdsbooks)$ export FLASK_ENV=development
    (cdsbooks)$ invenio run --with-threads --debugger
