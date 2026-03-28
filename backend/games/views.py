from decimal import Decimal

from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from drf_spectacular.utils import extend_schema, OpenApiParameter

from .models import Bet, CrashRound
from .serializers import (
    BetSerializer,
    DiceBetSerializer,
    MinesStartSerializer,
    MinesRevealSerializer,
    MinesCashoutSerializer,
    PlinkoBetSerializer,
    CrashBetSerializer,
    CrashCashoutSerializer,
    CrashRoundSerializer,
)
from .services import (
    play_dice,
    start_mines,
    reveal_mines_tile,
    cashout_mines,
    play_plinko,
    create_crash_round,
)


class BetPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


# --- Dice ---
@extend_schema(tags=['games'])
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def dice_bet(request):
    serializer = DiceBetSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    try:
        bet = play_dice(
            request.user.id,
            serializer.validated_data['currency'],
            serializer.validated_data['amount'],
            serializer.validated_data['direction'],
            serializer.validated_data['target'],
        )
        return Response(BetSerializer(bet).data)
    except ValueError as e:
        return Response({'detail': str(e)}, status=400)


# --- Mines ---
@extend_schema(tags=['games'])
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def mines_start(request):
    serializer = MinesStartSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    try:
        bet = start_mines(
            request.user.id,
            serializer.validated_data['currency'],
            serializer.validated_data['amount'],
            serializer.validated_data.get('mine_count', 5),
        )
        return Response(BetSerializer(bet).data)
    except ValueError as e:
        return Response({'detail': str(e)}, status=400)


@extend_schema(tags=['games'])
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def mines_reveal(request):
    serializer = MinesRevealSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    try:
        result = reveal_mines_tile(
            serializer.validated_data['bet_id'],
            request.user.id,
            serializer.validated_data['tile_index']
        )

        return Response({
            'is_mine': result['is_mine'],
            'bet': BetSerializer(result['bet']).data
        })

    except Bet.DoesNotExist:
        return Response({'detail': 'Bet not found'}, status=404)
    except ValueError as e:
        return Response({'detail': str(e)}, status=400)


@extend_schema(tags=['games'])
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def mines_cashout(request):
    serializer = MinesCashoutSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    try:
        bet = cashout_mines(serializer.validated_data['bet_id'], request.user.id)
        return Response(BetSerializer(bet).data)
    except Bet.DoesNotExist:
        return Response({'detail': 'Bet not found'}, status=404)
    except ValueError as e:
        return Response({'detail': str(e)}, status=400)


# --- Plinko ---
@extend_schema(tags=['games'])
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def plinko_bet(request):
    serializer = PlinkoBetSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    try:
        bet = play_plinko(
            request.user.id,
            serializer.validated_data['currency'],
            serializer.validated_data['amount'],
            serializer.validated_data['risk'],
        )
        return Response(BetSerializer(bet).data)
    except ValueError as e:
        return Response({'detail': str(e)}, status=400)


# --- Crash ---
@extend_schema(tags=['games'])
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def crash_bet(request):
    from wallets.services import debit_wallet
    from wallets.models import Transaction
    from django.db import transaction as db_transaction

    serializer = CrashBetSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    try:
        round_obj = CrashRound.objects.get(
            round_id=serializer.validated_data['round_id'],
            status=CrashRound.Status.BETTING
        )
    except CrashRound.DoesNotExist:
        return Response({'detail': 'Round not available'}, status=400)

    with db_transaction.atomic():
        bet_tx = debit_wallet(
            request.user.id,
            serializer.validated_data['currency'],
            serializer.validated_data['amount'],
            Transaction.Type.BET,
        )

        bet = Bet.objects.create(
            user=request.user,
            game_type=Bet.GameType.CRASH,
            currency_code=serializer.validated_data['currency'],
            amount=serializer.validated_data['amount'],
            status=Bet.Status.PENDING,
            metadata={'round_id': str(serializer.validated_data['round_id'])},
            bet_transaction_id=bet_tx.id,
        )

    return Response(BetSerializer(bet).data)


@extend_schema(tags=['games'])
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def crash_cashout(request):
    from wallets.services import credit_wallet
    from wallets.models import Transaction
    from django.db import transaction as db_transaction

    serializer = CrashCashoutSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    try:
        bet = Bet.objects.get(
            id=serializer.validated_data['bet_id'],
            user=request.user,
            status=Bet.Status.PENDING
        )
    except Bet.DoesNotExist:
        return Response({'detail': 'Bet not found'}, status=404)

    multiplier = Decimal(str(serializer.validated_data['multiplier']))
    payout = bet.amount * multiplier

    with db_transaction.atomic():
        win_tx = credit_wallet(
            request.user.id,
            bet.currency_code,
            payout,
            Transaction.Type.WIN,
        )

        bet.status = Bet.Status.CASHED_OUT
        bet.payout = payout
        bet.win_transaction_id = win_tx.id
        bet.save()

    return Response(BetSerializer(bet).data)


@extend_schema(tags=['games'])
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def crash_rounds(request):
    if not CrashRound.objects.filter(status=CrashRound.Status.BETTING).exists():
        create_crash_round()

    limit = min(int(request.query_params.get('limit', 20)), 50)
    rounds = CrashRound.objects.order_by('-created_at')[:limit]

    return Response(CrashRoundSerializer(rounds, many=True).data)


@extend_schema(tags=['games'])
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def crash_round_detail(request, round_id):
    try:
        round_obj = CrashRound.objects.get(round_id=round_id)
    except CrashRound.DoesNotExist:
        return Response({'detail': 'Not found'}, status=404)

    return Response(CrashRoundSerializer(round_obj).data)


# --- Bet history ---
@extend_schema(tags=['games'])
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def bet_list(request):
    qs = Bet.objects.filter(user=request.user).order_by('-created_at')

    game_type = request.query_params.get('game_type')
    if game_type:
        qs = qs.filter(game_type=game_type.upper())

    paginator = BetPagination()
    page = paginator.paginate_queryset(qs, request)

    return paginator.get_paginated_response(
        BetSerializer(page, many=True).data
    )
