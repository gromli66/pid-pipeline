"""
CLI Entry Point для P&ID Graph Builder.

Использование:
    python -m pid_graph_builder build --config config/default.yaml
    python -m pid_graph_builder build-single --equipment-mask ... --skeleton ... --output ...
"""

import argparse
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(
        prog='pid_graph_builder',
        description='P&ID Graph Builder - Построение графа связности трубопроводов'
    )
    
    subparsers = parser.add_subparsers(dest='command', help='Доступные команды')
    
    # ===== BUILD (batch) =====
    build_parser = subparsers.add_parser(
        'build',
        help='Построить графы для всех схем в директории'
    )
    build_parser.add_argument(
        '--config', '-c',
        default='config/default.yaml',
        help='Путь к конфигурационному файлу (default: config/default.yaml)'
    )
    build_parser.add_argument(
        '--limit',
        type=int,
        default=None,
        help='Ограничить количество обрабатываемых файлов (для тестирования)'
    )
    build_parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Подробный вывод'
    )
    build_parser.add_argument(
        '--debug',
        action='store_true',
        help='Отладочный вывод (детали трассировки, классификации и т.д.)'
    )
    
    # ===== BUILD-SINGLE =====
    single_parser = subparsers.add_parser(
        'build-single',
        help='Построить граф для одной схемы'
    )
    single_parser.add_argument(
        '--equipment-mask',
        required=True,
        help='Путь к маске оборудования (PNG)'
    )
    single_parser.add_argument(
        '--connection-mask',
        required=True,
        help='Путь к маске connections (PNG)'
    )
    single_parser.add_argument(
        '--bridge-mask',
        required=True,
        help='Путь к маске bridges (PNG)'
    )
    single_parser.add_argument(
        '--skeleton',
        required=True,
        help='Путь к скелету труб (PNG)'
    )
    single_parser.add_argument(
        '--output',
        required=True,
        help='Путь к выходной директории'
    )
    single_parser.add_argument(
        '--original-image',
        default=None,
        help='Путь к оригинальному изображению для фона (опционально)'
    )
    single_parser.add_argument(
        '--yolo-labels',
        default=None,
        help='Путь к YOLO .txt файлу с разметкой классов оборудования'
    )
    single_parser.add_argument(
        '--name',
        default=None,
        help='Имя схемы (по умолчанию: из имени файла skeleton)'
    )
    single_parser.add_argument(
        '--config', '-c',
        default=None,
        help='Путь к конфигурационному файлу (для параметров визуализации)'
    )
    single_parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Подробный вывод'
    )
    single_parser.add_argument(
        '--debug',
        action='store_true',
        help='Отладочный вывод (детали трассировки, классификации и т.д.)'
    )
    single_parser.add_argument(
        '--coco',
        default=None,
        help='Путь к COCO JSON файлу с разметкой'
    )
    # Парсинг аргументов
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    # Импорт и выполнение команд
    if args.command == 'build':
        from .commands.build import run_build
        run_build(args)

    elif args.command == 'build-single':
        from .commands.build_single import run_build_single
        run_build_single(args)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()