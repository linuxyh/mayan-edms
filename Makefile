.PHONY: clean-pyc clean-build


help:
	@echo
	@echo "clean-build - Remove build artifacts."
	@echo "clean-pyc - Remove Python artifacts."
	@echo "clean - Remove Python and build artifacts."

	@echo "test MODULE=<python module name> - Run tests for a single App, module or test class."
	@echo "test-all - Run all tests."
	@echo "docs_serve - Run the livehtml documentation generator."

	@echo "translations_make - Refresh all translation files."
	@echo "translations_compile - Compile all translation files."
	@echo "translations_push - Upload all translation files to Transifex."
	@echo "translations_pull - Download all translation files from Transifex."

	@echo "requirements_dev - Install development requirements."
	@echo "requirements_docs - Install documentation requirements."
	@echo "requirements_testing - Install testing requirements."

	@echo "sdist - Build the source distribution package."
	@echo "wheel - Build the wheel distribution package."
	@echo "release - Package (sdist and wheel) and upload a release."

	@echo "runserver - Run the development server."
	@echo "shell_plus - Run the shell_plus command."


# Cleaning

clean: clean-build clean-pyc

clean-build:
	rm -fr build/
	rm -fr dist/
	rm -fr *.egg-info

clean-pyc:
	find . -name '*.pyc' -exec rm -f {} +
	find . -name '*.pyo' -exec rm -f {} +
	find . -name '*~' -exec rm -f {} +


# Testing

test:
	./manage.py test $(MODULE) --settings=mayan.settings.testing --nomigrations

test-all:
	./manage.py runtests --settings=mayan.settings.testing --nomigrations


# Documentation

docs_serve:
	cd docs;make livehtml


# Translations

translations_make:
	contrib/scripts/process_messages.py -m

translations_compile:
	contrib/scripts/process_messages.py -c

translations_push:
	tx push -s

translations_pull:
	tx pull


# Requirements

requirements_dev:
	pip install -r requirements/development.txt

requirements_docs:
	pip install -r requirements/documentation.txt

requirements_testing:
	pip install -r requirements/testing.txt


# Releases


test_release: clean wheel
	twine upload dist/* -r testpypi
	@echo "Test with: pip install -i https://testpypi.python.org/pypi mayan-edms"

release: clean wheel
	twine upload dist/* -r pypi

sdist: clean
	python setup.py sdist
	ls -l dist

wheel: clean sdist
	pip wheel --no-index --no-deps --wheel-dir dist dist/*.tar.gz
	ls -l dist


# Dev server

runserver:
	./manage.py runserver

shell_plus:
	./manage.py shell_plus --settings=mayan.settings.development
